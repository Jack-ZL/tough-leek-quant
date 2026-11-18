"""PR5 g1: AI 财务分析的市场维 (docs/hk-market-plan.md §12 财务概况)。

背景 —— 这一组测试钉的是**同一类缺陷的第四次出现** (前三次: f1 日 K、f3 财务
读取器、levels 右端点): ``financial_analyzer`` 原本只读 A 股财务宽表, 港股代码
在里面一行都没有, 于是对 ``00700.HK`` 输出

    标的 00700.HK 暂无任何财务数据,请先同步财务表

而财务概况页那个「同步」按钮走的是 A 股 ``POST /api/financials/sync/{table}`` ——
**用户照做永远修不好**, 因为港股财报存在 ``financials_hk/``, 得走
``POST /api/hk/sync_financials``。提示词/错误文案把用户导向一个无效操作,
比直接报错更难发现。

四条主线:
1. **A 股逐字不变**: 系统提示词、用户提示词、错误文案、``_summarize`` 输出
   全部与引入 market 维之前**字节相同** (CONTRIBUTING §9)。
2. **港股读自己的表**: 分派到 ``hk_latest_financials``, 且**不碰** A 股宽表。
3. **港股口径写进提示词**: 无股本表 / 列集是子集 / 混合币种 / 无公告日 /
   金融股字段族 / 报告期口径 —— 六条都来自 PR2b 落库数据的**实测**结论。
4. **不新增数字闸门白名单**: 港股提示词相对 A 股不得多出任何可入池数字
   (与 ``test_market_guardrail.test_full_system_prompt_leaks_no_extra_numbers_for_hk``
   同一条纪律)。
"""

from __future__ import annotations

import inspect
import json

import polars as pl
import pytest

from app.market import Market
from app.services import financial_analyzer as fa
from app.services.ai_number_gate import scan_text_numbers
from app.services.market_guardrail import guardrail_prompt

HK = "00700.HK"
CN = "600519.SH"

#: 港股规范化表的四个键 —— **没有 A 股的 ``shares``** (港股宽表不提供股本表)。
HK_TABLES = ("metrics", "income", "balance_sheet", "cash_flow")


def _hk_frames(limit_rows: int = 1) -> dict[str, pl.DataFrame]:
  """构造 ``hk_latest_financials`` 形态的返回值 (四张表, 含港股专有列)。"""
  base = {
    "symbol": [HK] * limit_rows,
    "period_end": ["2026-09-30"] * limit_rows,
    "announce_date": [None] * limit_rows,
    "org_type": ["一般企业"] * limit_rows,
    "pricing_currency": ["HKD"] * limit_rows,
  }
  return {
    "metrics": pl.DataFrame({**base, "roe": [15.0], "gross_margin": [56.8]}),
    "income": pl.DataFrame({**base, "revenue": [608579000000.0]}),
    "balance_sheet": pl.DataFrame({**base, "total_assets": [2346717000000.0]}),
    "cash_flow": pl.DataFrame({**base, "net_operating_cash_flow": [156713000000.0]}),
  }


# ===== 1. A 股逐字不变 =====

def test_cn_system_prompt_is_the_compat_constant():
  """``_build_system_prompt(Market.CN)`` 逐字等于兼容常量 ``_SYSTEM_PROMPT``。"""
  assert fa._build_system_prompt(Market.CN) == fa._SYSTEM_PROMPT
  assert fa._build_system_prompt("cn") == fa._SYSTEM_PROMPT
  assert fa._build_system_prompt() == fa._SYSTEM_PROMPT


def test_cn_prompt_keeps_ashare_wording_and_no_hk_sections():
  """A 股提示词仍是「A 股投研经验 / 对照 A 股常识」, 且不含任何港股段落。"""
  p = fa._SYSTEM_PROMPT
  assert "15 年 A 股投研经验的资深财务分析师" in p
  assert "对照 A 股常识判断水平" in p
  assert "港股财报口径" not in p
  assert guardrail_prompt(Market.CN) == ""  # A 股护栏本就是空串


def test_cn_user_prompt_keeps_yuan_unit_and_defaults_to_cn():
  """``market`` 加在参数表末尾且默认 A 股 —— 既有三参数调用方逐字不变。"""
  payload = {t: [] for t in ("metrics", "income", "balance_sheet", "cash_flow", "shares")}
  three_args = fa._build_user_prompt(payload, CN, "关注毛利率")
  explicit_cn = fa._build_user_prompt(payload, CN, "关注毛利率", Market.CN)
  assert three_args == explicit_cn
  assert "金额单位为元" in three_args
  assert "编报币种" not in three_args


def test_cn_summarize_lists_all_five_tables():
  """A 股摘要仍是五张表 (含 ``shares``), 顺序即 ``FINANCIAL_TABLES``。"""
  payload = {t: [] for t in ("metrics", "income", "balance_sheet", "cash_flow", "shares")}
  out = fa._summarize(payload)
  assert out.split(" · ") == [f"{t}: 无数据" for t in payload]
  assert "shares: 无数据" in out


def test_cn_loaders_still_scan_five_ashare_tables(monkeypatch, tmp_path):
  """A 股读取器照旧扫五张表, 一张不多一张不少。"""
  calls: list[str] = []

  def _fake(_data_dir, table):
    calls.append(table)
    return pl.DataFrame()

  monkeypatch.setattr(fa, "get_financial_df", _fake)
  assert fa._load_stock_financials(tmp_path, CN) == {t: [] for t in calls}
  assert calls == ["metrics", "income", "balance_sheet", "cash_flow", "shares"]


# ===== 2. 港股读自己的表 =====

def test_hk_loader_does_not_scan_ashare_tables(monkeypatch, tmp_path):
  """🔴 港股**不碰** ``get_financial_df``。

  这是本文件最核心的回归: 旧实现对港股也扫 A 股宽表, 白扫全表拿到空帧,
  再据此输出「请先同步财务表」这条修不好的建议。
  """
  def _boom(_data_dir, table):
    raise AssertionError(f"港股不应读 A 股财务表 {table}")

  monkeypatch.setattr(fa, "get_financial_df", _boom)
  monkeypatch.setattr(
    "app.services.financial_sync_hk.hk_latest_financials",
    lambda _d, _s, *, limit=8: _hk_frames(),
  )
  out = fa._load_stock_financials(tmp_path, HK)
  assert set(out) == set(HK_TABLES)


def test_hk_loader_reads_hk_store_with_max_periods(monkeypatch, tmp_path):
  """港股走 ``hk_latest_financials``, 期数上限与 A 股共用 ``_MAX_PERIODS``。"""
  seen: dict[str, object] = {}

  def _fake(_data_dir, symbol, *, limit=8):
    seen["symbol"] = symbol
    seen["limit"] = limit
    return _hk_frames()

  # 函数内 import → 打桩打在源模块上才生效
  monkeypatch.setattr("app.services.financial_sync_hk.hk_latest_financials", _fake)
  out = fa._load_stock_financials(tmp_path, HK)

  assert seen == {"symbol": HK, "limit": fa._MAX_PERIODS}
  assert set(out) == set(HK_TABLES)
  assert "shares" not in out  # 港股没有股本表, 不补空键冒充
  # symbol 被剥掉 (不重复回传), 港股专有列保留 (org_type / pricing_currency
  # 是模型判断金融股字段族与币种口径的唯一依据)
  assert out["metrics"][0] == {
    "period_end": "2026-09-30",
    "announce_date": None,
    "org_type": "一般企业",
    "pricing_currency": "HKD",
    "roe": 15.0,
    "gross_margin": 56.8,
  }


def test_hk_loader_cleans_nan_and_inf(monkeypatch, tmp_path):
  """NaN / Inf → null: 否则 ``json.dumps`` 直接抛错, 整个流式分析 500。"""
  frames = _hk_frames()
  frames["metrics"] = frames["metrics"].with_columns(
    pl.lit(float("nan")).alias("roe"), pl.lit(float("inf")).alias("gross_margin")
  )
  monkeypatch.setattr(
    "app.services.financial_sync_hk.hk_latest_financials",
    lambda _d, _s, *, limit=8: frames,
  )
  row = fa._load_stock_financials(tmp_path, HK)["metrics"][0]
  assert row["roe"] is None
  assert row["gross_margin"] is None
  json.dumps(row)  # 不抛即为通过


def test_hk_unsynced_yields_four_empty_tables(monkeypatch, tmp_path):
  """未同步时四张表都是空列表 (``hk_latest_financials`` 返回带完整列集的空帧)。"""
  monkeypatch.setattr(
    "app.services.financial_sync_hk.hk_latest_financials",
    lambda _d, _s, *, limit=8: {
      t: pl.DataFrame(schema={"symbol": pl.Utf8}) for t in HK_TABLES
    },
  )
  out = fa._load_stock_financials(tmp_path, HK)
  assert out == {t: [] for t in HK_TABLES}
  assert sum(len(v) for v in out.values()) == 0


def test_load_stock_financials_signature_stays_two_args():
  """签名**刻意**保持 ``(data_dir, symbol)``。

  与 ``stock_analyzer._load_financials`` 同一条纪律: 市场由 symbol 唯一决定,
  加参数会让调用方与打桩静默失配。
  """
  assert list(inspect.signature(fa._load_stock_financials).parameters) == ["data_dir", "symbol"]


def test_user_prompt_market_param_is_last_and_defaults_cn():
  """``market`` 在参数表**末尾**且默认 A 股 (既有三参数调用方不受影响)。"""
  params = list(inspect.signature(fa._build_user_prompt).parameters)
  assert params == ["fins", "symbol", "focus", "market"]
  assert inspect.signature(fa._build_user_prompt).parameters["market"].default is Market.CN


# ===== 3. 注册表完整性与 fail-closed =====

def test_every_registry_covers_every_market():
  """新增市场必须四张口径表 + 读取器全部注册。"""
  for name, reg in (
    ("_MARKET_NORM_RULE", fa._MARKET_NORM_RULE),
    ("_MARKET_FIN_NOTE", fa._MARKET_FIN_NOTE),
    ("_MARKET_EMPTY_HINT", fa._MARKET_EMPTY_HINT),
    ("_MARKET_DATA_LINE", fa._MARKET_DATA_LINE),
    ("_FINANCIAL_LOADERS", fa._FINANCIAL_LOADERS),
  ):
    assert set(reg) == set(Market), f"{name} 未覆盖全部市场"


@pytest.mark.parametrize("reg", [
  "_MARKET_NORM_RULE", "_MARKET_FIN_NOTE", "_MARKET_EMPTY_HINT",
])
def test_unregistered_market_raises_instead_of_falling_back_to_cn(monkeypatch, reg):
  """🔴 漏注册当场 ``ValueError``, 不静默按 A 股口径出一份「看起来对」的报告。"""
  monkeypatch.setattr(
    fa, reg, {k: v for k, v in getattr(fa, reg).items() if k is not Market.HK}
  )
  with pytest.raises(ValueError, match="未注册 AI 财务提示词口径"):
    fa._build_system_prompt(Market.HK)


def test_loader_registry_unregistered_market_raises(monkeypatch, tmp_path):
  monkeypatch.setattr(
    fa, "_FINANCIAL_LOADERS",
    {k: v for k, v in fa._FINANCIAL_LOADERS.items() if k is not Market.HK},
  )
  with pytest.raises(ValueError, match="未注册 AI 财务读取器"):
    fa._load_stock_financials(tmp_path, HK)


# ===== 4. 港股提示词口径 =====

def test_hk_persona_and_norm_rule_replace_the_ashare_ones():
  p = fa._build_system_prompt(Market.HK)
  assert "15 年港股投研经验的资深财务分析师" in p
  assert "15 年 港股" not in p  # 纯中文称谓不带前导空格
  assert "对照港股与 IFRS 口径判断水平" in p
  # 🔴 A 股那两处措辞必须**消失**, 不能只是追加一段港股说明
  assert "15 年 A 股投研经验" not in p
  assert "对照 A 股常识判断水平" not in p


def test_hk_prompt_carries_all_six_report_caveats():
  """六条口径逐条在场 —— 每条都对应一个实测会让模型说错话的坑。"""
  p = fa._build_system_prompt(Market.HK)
  for frag in (
    "没有 A 股的股本表",        # ① shares 键不存在 ≠ 缺失
    "列集是 A 股的子集",        # ② 周转率类键根本不出现, 不许推算
    "币种是混合的",             # ③ 金额是编报币种, 估值列是港币
    "禁止**默认按人民币表述",   # ③ 续: 不许自行换算汇率
    "``announce_date`` 恒为空",  # ④ 无公告日是数据源口径
    "金融股字段族",             # ⑤ org_type 决定科目有无
    "不要把中报数据年化",       # ⑥ 港股中报是半年度正式财报
  ):
    assert frag in p, f"港股财报口径缺: {frag}"


def test_hk_prompt_appends_shared_market_guardrail():
  """市场事实护栏 (T+0 / 无涨跌停 / 延时) 仍由 ``market_guardrail`` 单一来源提供。"""
  p = fa._build_system_prompt(Market.HK)
  assert guardrail_prompt(Market.HK) in p
  # 层次: 财务口径附注在护栏**之前** (领域口径 → 市场事实 → 输出契约)
  assert p.index("港股财报口径") < p.index("港股市场口径护栏")


def test_hk_user_prompt_uses_reporting_currency_wording():
  payload = {t: [] for t in HK_TABLES}
  p = fa._build_user_prompt(payload, HK, "", Market.HK)
  assert "编报币种" in p
  assert "金额单位为元" not in p
  assert "港股财报口径" in p  # 指回系统提示词那一节, 不在用户消息里重复长篇


def test_hk_summarize_has_no_shares_row():
  """🔴 港股摘要不得出现 ``shares: 无数据``。

  港股**根本没有股本表**; 写成「无数据」等于告诉模型「这里本该有但缺了」,
  诱导它去推断总股本 / 流通股。
  """
  out = fa._summarize({t: [] for t in HK_TABLES})
  assert "shares" not in out
  assert out.split(" · ") == [f"{t}: 无数据" for t in HK_TABLES]


# ===== 5. 数字闸门白名单不扩大 =====

def test_hk_prompt_leaks_no_extra_numbers():
  """端到端: 港股系统提示词相对 A 股**不新增**任何白名单数字。

  系统提示词会进 ``collect_whitelist(texts=...)``; 多一个数字就等于给模型
  编造同值数字发通行证。港股阈值刻意沿用 A 股那组阿拉伯数字 (而不是改写成
  中文数字): 阈值是模型下判断时要**引用**的标准, 不入池会被闸门打码。
  """
  cn = set(scan_text_numbers(fa._build_system_prompt(Market.CN)))
  hk = set(scan_text_numbers(fa._build_system_prompt(Market.HK)))
  assert hk - cn == set(), f"港股财务提示词新增白名单数字: {sorted(hk - cn)}"


def test_hk_user_prompt_leaks_no_extra_numbers():
  """用户提示词里那句数据说明: 港股相对 A 股不新增白名单数字。

  🔴 只扫 ``_MARKET_DATA_LINE``, 不扫整条用户消息: 消息里还有
  ``标的标准代码: 00700.HK``, 代码里的数字属于**本轮标的数据**而非提示词文案,
  拿 A 股 ``600519`` 与港股 ``00700`` 对比只会得到一条与口径无关的差异
  (实测正是这条把断言打红的)。
  """
  cn = set(scan_text_numbers(fa._MARKET_DATA_LINE[Market.CN]))
  hk = set(scan_text_numbers(fa._MARKET_DATA_LINE[Market.HK]))
  assert hk - cn == set(), f"港股数据说明新增白名单数字: {sorted(hk - cn)}"


# ===== 6. 空数据错误文案 (用户照做能修好) =====

async def _drain(data_dir, symbol):
  return [json.loads(line) async for line in fa.analyze_financials_stream(data_dir, symbol)]


async def test_hk_empty_data_points_at_the_hk_sync(tmp_path, monkeypatch):
  """🔴 港股的错误文案必须指向**港股**同步入口。

  A 股那句「请先同步财务表」对港股是错误建议: 财务概况页的 A 股同步走
  ``POST /api/financials/sync/{table}``, 根本不写 ``financials_hk/``。
  """
  monkeypatch.setattr(
    "app.services.financial_sync_hk.hk_latest_financials",
    lambda _d, _s, *, limit=8: {t: pl.DataFrame(schema={"symbol": pl.Utf8}) for t in HK_TABLES},
  )
  evs = await _drain(tmp_path, HK)
  assert [e["type"] for e in evs] == ["error"]
  msg = evs[0]["message"]
  assert msg.startswith(f"标的 {HK} 暂无任何财务数据,")
  assert "同步港股财务表" in msg
  assert "分开存储" in msg


async def test_cn_empty_data_message_is_unchanged(tmp_path, monkeypatch):
  """A 股文案逐字保留 (既有前端与用户习惯都依赖它)。"""
  monkeypatch.setattr(fa, "get_financial_df", lambda _d, _t: pl.DataFrame())
  evs = await _drain(tmp_path, CN)
  assert evs == [{
    "type": "error",
    "message": f"标的 {CN} 暂无任何财务数据,请先同步财务表",
  }]
