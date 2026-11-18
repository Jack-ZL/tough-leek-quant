"""机器人入口的港股支持 (方案 §9.5): 标的解析 + 报告抬头的市场口径行。

§9.5 原文: 「企业微信 / 钉钉智能机器人**共用现有入口**, 消息模板增加:
市场标识 · 延时标注 · 货币单位 (``HK$``)。」

本文件钉三件事:

1. **名称解析能命中港股**, 且港股排在 A 股三张维表**之后** —— A 股与港股简称
   存在重名 (如「中国银行」), 顺序反了就会把 A 股用户既有查询解析到港股去,
   违反 CONTRIBUTING §9「既有 A 股测试不得修改即须通过」。
2. **纯 A 股调用零额外开销**: A 股命中即短路, 不去读港股维表 (同
   ``app/api/watchlist.py:_name_map_with_hk`` 与 ``tests/test_instrument_search.py``
   的既有立论)。没同步过港股维表的部署不该因此吃 warning。
3. **两个 bot 的市场口径行逐字节一致**。两份 ``_build_analysis_report`` 是历史
   现状 (企微 TAB 缩进 / 钉钉 4 空格), 孪生实现最怕单边漂移 —— 漂移的后果是
   同一个港股代码在两个渠道给出不同的延时/货币标注, 而这类文案恰恰是防误读的。

🔴 报告抬头**不是** AI 提示词: 它是 ``pop_gate_footer`` 之后组装给用户看的成品,
不进数字闸门白名单, 所以这里写阿拉伯数字「15 分钟延时」是正确且必要的
(对比 ``app/services/market_guardrail.py``: 提示词里必须写「十五分钟」)。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.market import Market, currency_symbol
from app.services import dingtalk_bot_service as d
from app.services import wecom_bot_service as w

BOTS = (w, d)


class _Repo:
  """最小 repo 桩: A 股三张维表 + 可选港股维表。

  ``hk_calls`` 钉「纯 A 股调用方不得触发港股维表读取」。``hk=None`` 时**不定义**
  ``get_hk_instruments`` 方法本身 (而不是定义一个返回空表的方法) —— 这才是纯 A 股
  部署与既有测试桩的真实形状, 用来验证 bot 侧的 ``getattr`` 探测。
  """

  def __init__(
    self,
    stocks: pl.DataFrame | None = None,
    etfs: pl.DataFrame | None = None,
    index: pl.DataFrame | None = None,
    hk: pl.DataFrame | None = None,
  ) -> None:
    self._stocks = pl.DataFrame() if stocks is None else stocks
    self._etfs = pl.DataFrame() if etfs is None else etfs
    self._index = pl.DataFrame() if index is None else index
    self._hk = hk
    self.hk_calls = 0
    if hk is not None:
      # 只有拿到港股表时才"长出"这个方法, 模拟纯 A 股部署没有该读取器
      self.get_hk_instruments = self._get_hk_instruments

  def get_instruments(self) -> pl.DataFrame:
    return self._stocks

  def get_etf_instruments(self) -> pl.DataFrame:
    return self._etfs

  def get_instruments_asset(self, _asset_type: str) -> pl.DataFrame:
    return self._index

  def _get_hk_instruments(self) -> pl.DataFrame:
    self.hk_calls += 1
    return self._hk


def _frame(rows: list[tuple[str, str]]) -> pl.DataFrame:
  return pl.DataFrame(
    {"symbol": [r[0] for r in rows], "name": [r[1] for r in rows]},
    schema={"symbol": pl.Utf8, "name": pl.Utf8},
  )


def _ashare_repo(**kw) -> _Repo:
  return _Repo(stocks=_frame([("600519.SH", "贵州茅台")]), **kw)


# ===== 1. currency_symbol: 展示符号与 ISO 码分工 =====

@pytest.mark.parametrize(("market", "expected"), [
  (Market.CN, "¥"),
  (Market.HK, "HK$"),
  ("hk", "HK$"),          # 别名/大小写走 parse_market
  ("HK", "HK$"),
])
def test_currency_symbol_known_markets(market, expected):
  assert currency_symbol(market) == expected


def test_currency_symbol_defaults_to_cn():
  """**不传参**才等价 CN (与 rules_for 同一约定)。"""
  assert currency_symbol() == "¥"


@pytest.mark.parametrize("bad", ["XX", "", 123, object()])
def test_currency_symbol_fail_closed_on_unknown(bad):
  """无法识别的市场**抛错**, 不兜底成 CN。

  静默兜底会给港股报告打上 ``¥`` —— ¥ 与 HK$ 差约 7.8 倍, 是典型的
  「看起来对但错」(CONTRIBUTING §3 / §12)。
  """
  with pytest.raises(ValueError):
    currency_symbol(bad)


def test_currency_symbol_none_raises_like_rules_for():
  """🔴 显式传 ``None`` 也抛错, 与 :func:`rules_for` 的契约一致。

  「market 尚未确定」和「market 是 A 股」是两件事。缺省语义只由**默认参数**
  表达 (``currency_symbol()``), 不由 ``None`` 表达 —— 否则上游 infer 失败返回
  的 None 会静默变成 ¥ 计价。
  """
  from app.market import rules_for

  with pytest.raises(ValueError):
    currency_symbol(None)
  with pytest.raises(ValueError):
    rules_for(None)


def test_currency_symbol_is_not_the_iso_code():
  """符号与 ISO 码是两个字段, 不得互相顶替 (数据契约按 ISO 码比对)。"""
  from app.market import rules_for

  assert currency_symbol(Market.HK) == "HK$"
  assert rules_for(Market.HK).currency == "HKD"
  assert currency_symbol(Market.CN) == "¥"
  assert rules_for(Market.CN).currency == "CNY"


# ===== 2. 名称解析: 港股可命中, 但排在 A 股之后 =====

@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_resolve_symbol_finds_hk_by_name(bot):
  repo = _ashare_repo(hk=_frame([("00700.HK", "腾讯控股")]))
  assert bot._resolve_symbol(repo, "腾讯控股") == "00700.HK"
  assert repo.hk_calls == 1


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_resolve_symbol_finds_hk_by_fuzzy_name(bot):
  repo = _ashare_repo(hk=_frame([("00700.HK", "腾讯控股")]))
  assert bot._resolve_symbol(repo, "腾讯") == "00700.HK"


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_ashare_wins_over_hk_on_duplicate_name(bot):
  """🔴 重名时 A 股优先: 既有 A 股用户的解析结果必须逐字不变。

  「中国银行」A+H 两地上市, 简称完全相同。港股若排在前面, 老用户发
  「中国银行」会突然从 601988.SH 变成 03988.HK —— 静默改语义。
  """
  repo = _Repo(
    stocks=_frame([("601988.SH", "中国银行")]),
    hk=_frame([("03988.HK", "中国银行")]),
  )
  assert bot._resolve_symbol(repo, "中国银行") == "601988.SH"


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_pure_ashare_hit_never_reads_hk_table(bot):
  """A 股命中即短路, 港股维表一次都不读 (零额外开销 / 零无谓 warning)。"""
  repo = _ashare_repo(hk=_frame([("00700.HK", "腾讯控股")]))
  assert bot._resolve_symbol(repo, "贵州茅台") == "600519.SH"
  assert repo.hk_calls == 0


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_repo_without_hk_reader_still_works(bot):
  """🔴 纯 A 股部署 (repo 没有 ``get_hk_instruments``) 不得 AttributeError。

  既有测试桩 ``tests/test_dingtalk_bot_message.py:_Repo`` 就是这个形状, 且按
  CONTRIBUTING §9 必须**不修改**即通过 —— 所以 bot 侧只能用 getattr 探测。
  """
  repo = _ashare_repo()  # hk=None ⇒ 不定义 get_hk_instruments
  assert not hasattr(repo, "get_hk_instruments")
  assert bot._resolve_symbol(repo, "贵州茅台") == "600519.SH"
  assert bot._resolve_symbol(repo, "不存在") is None  # 未找到走提示, 不抛


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_candidate_frames_skip_absent_hk_reader_without_raising(bot):
  """🔴 直接钉 `_candidate_name_frames`: 缺港股读取器时**不抛** AttributeError。

  只测 `_resolve_symbol` 的返回值钉不住这一点 —— 它的 `except Exception` 会把
  AttributeError 吞成「未找到」, 于是「优雅跳过港股」与「读港股维表时崩了」在
  返回值上**完全同形**。变异测试实测过这个洞: 把 `getattr` 探测换成直接
  `repo.get_hk_instruments()`, 全部 `_resolve_symbol` 断言照样绿。

  缺读取器是纯 A 股部署的**正常形状**, 不是异常, 不该靠兜底 except 掩盖 ——
  今天靠「港股排最后 + 宽 except」侥幸等价, 哪天调整表顺序或收窄 except,
  A 股名称解析就会整体静默失效。
  """
  repo = _ashare_repo()  # hk=None ⇒ 不定义 get_hk_instruments
  assert not hasattr(repo, "get_hk_instruments")
  frames = list(bot._candidate_name_frames(repo))  # 抽干生成器: 有 AttributeError 就在这里炸
  assert len(frames) == 3  # A 股股票 / ETF / 指数, 港股被跳过而不是报错


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_candidate_frames_append_hk_last(bot):
  """有港股读取器时产出四张表, 且港股在**最后** (A 股优先的顺序契约)。"""
  repo = _ashare_repo(hk=_frame([("00700.HK", "腾讯控股")]))
  frames = list(bot._candidate_name_frames(repo))
  assert len(frames) == 4
  assert repo.hk_calls == 1
  assert frames[-1]["symbol"].to_list() == ["00700.HK"]


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_index_still_resolves_when_hk_reader_absent(bot):
  """缺港股读取器不影响 A 股三张表**全部**被查到 (指数是第三张)。"""
  repo = _Repo(index=_frame([("000001.SH", "上证指数")]))
  assert not hasattr(repo, "get_hk_instruments")
  assert bot._resolve_symbol(repo, "上证指数") == "000001.SH"


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_hk_reader_raising_degrades_to_not_found(bot):
  """港股维表读取抛错时降级为「未找到」, 不把异常抛回消息处理链。"""
  class _Boom(_Repo):
    def get_hk_instruments(self):
      raise RuntimeError("维表损坏")

  assert bot._resolve_symbol(_Boom(stocks=_frame([("600519.SH", "贵州茅台")])), "腾讯") is None


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_hk_frame_missing_columns_is_skipped(bot):
  """港股维表缺 symbol/name 列时跳过, 不让 polars 抛列不存在。"""
  repo = _ashare_repo(hk=pl.DataFrame({"code": ["00700"], "label": ["腾讯控股"]}))
  assert bot._resolve_symbol(repo, "腾讯") is None
  assert bot._resolve_symbol(repo, "贵州茅台") == "600519.SH"


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_etf_and_index_still_resolve(bot):
  """既有 A 股 ETF / 指数回退路径不受港股改动影响。"""
  repo = _Repo(
    etfs=_frame([("510300.SH", "沪深300ETF")]),
    index=_frame([("000001.SH", "上证指数")]),
    hk=_frame([("00700.HK", "腾讯控股")]),
  )
  assert bot._resolve_symbol(repo, "沪深300ETF") == "510300.SH"
  assert bot._resolve_symbol(repo, "上证指数") == "000001.SH"


# ===== 3. 报告抬头的市场口径行 =====

@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_hk_note_carries_label_delay_and_currency(bot):
  """§9.5 要求的三件套: 市场标识 · 延时标注 · 货币单位。"""
  note = bot._market_note("00700.HK")
  assert "港股" in note            # 市场标识 (HK_RULES.label)
  assert "15 分钟延时" in note      # 延时标注 (HK_RULES.quote_delay_minutes)
  assert "HK$" in note             # 货币单位
  assert "HKD" in note             # ISO 码同时给出, 便于对账
  assert "实时行情" not in note


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_ashare_note_says_realtime_and_cny(bot):
  """A 股也标注 (延时 0 → 「实时行情」), 两个市场可对照才防误读。"""
  note = bot._market_note("600519.SH")
  assert "A股" in note
  assert "实时行情" in note
  assert "¥" in note
  assert "CNY" in note
  assert "HK$" not in note
  assert "分钟延时" not in note


def test_market_note_is_byte_identical_across_bots():
  """🔴 孪生实现防漂移: 同一代码在企微/钉钉必须给出逐字节相同的口径行。"""
  for sym in ("600519.SH", "00700.HK", "510300.SH", "03988.HK"):
    assert w._market_note(sym) == d._market_note(sym), sym


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_market_note_reads_rules_not_hardcoded(bot, monkeypatch):
  """口径行读规则表**当次值**, 不写死 15 / HK$ (改表即改文案)。"""
  import dataclasses

  import app.market as m

  # MarketTradingRules 是 frozen dataclass (只读契约), 改写要走 dataclasses.replace
  monkeypatch.setitem(m.RULES_BY_MARKET, Market.HK, dataclasses.replace(
    m.HK_RULES, quote_delay_minutes=42, label="港股(测试)", currency="USD",
  ))
  monkeypatch.setitem(m._CURRENCY_SYMBOL, Market.HK, "US$")
  note = bot._market_note("00700.HK")
  assert "42 分钟延时" in note
  assert "港股(测试)" in note
  assert "US$" in note and "USD" in note
  assert "15" not in note and "HK$" not in note


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_report_embeds_market_note(bot):
  """口径行真的进了成品报告, 且紧跟标的行 (抬头区, 不在正文里)。"""
  rpt = bot._build_analysis_report("正文内容", "腾讯控股", "00700.HK", 1.0)
  assert bot._market_note("00700.HK") in rpt
  assert rpt.index("00700.HK") < rpt.index("港股") < rpt.index("正文内容")


@pytest.mark.parametrize("bot", BOTS, ids=lambda b: b.__name__.rsplit(".", 1)[-1])
def test_report_keeps_existing_sections(bot):
  """加口径行不得挤掉既有抬头字段 / 免责声明 / 模型行。"""
  rpt = bot._build_analysis_report("正文", "贵州茅台", "600519.SH", 12.3, model="deepseek-chat")
  for expected in ("AI 个股分析报告", "贵州茅台", "600519.SH", "分析时间",
                   "分析用时", "deepseek-chat", "入市需谨慎", "正文"):
    assert expected in rpt, expected
