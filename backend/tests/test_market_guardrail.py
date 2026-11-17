"""PR4 f3: 市场口径护栏 (docs/hk-market-plan.md §9.1)。

验收口径来自方案 §14:「港股 prompt 含护栏 6 条; 模型不把 null 连板数当『没涨停』
(**可用固定 fixture 断言 prompt 内容**)」—— 断言**提示词文本**, 不断言模型行为。

三条主线:
1. **A 股逐字不变**: 护栏对 CN 返回空串, 系统提示词与引入 market 维之前完全一致。
2. **港股 6 条齐全**, 且所有市场事实**读规则表**而不是写死 15 / 100 / HKD。
3. **财务缺失文案分市场**: A 股「Free 模式或尚未同步」, 港股「财报未接入, 不是
   Free 模式限制」—— 后者是错误建议的纠正 (用户去开 Free 模式永远修不好)。
"""

from __future__ import annotations

import dataclasses

import pytest

from app.market import HK_RULES, Market
from app.services import market_guardrail as mg
from app.services import stock_analyzer as sa

HK = "00700.HK"
CN = "600519.SH"

#: 系统提示词第 4 节靠这句**原文**匹配来决定输出「财务面接入中」段落。
#: 两个市场的兜底文案都必须以它开头, 否则第 4 节静默不触发。
FIN_TRIGGER = "该标的暂无财务数据"


# ===== 1. A 股逐字不变 (CONTRIBUTING §9: 既有 A 股行为原样保留) =====

def test_cn_guardrail_is_empty_string():
  """A 股不注入护栏 —— 系统提示词本就是按 A 股写的, 无需纠偏。"""
  assert mg.guardrail_prompt(Market.CN) == ""
  assert mg.guardrail_prompt("cn") == ""


def test_cn_system_prompt_byte_identical_to_compat_constant():
  """``_build_system_prompt(False)`` 必须逐字等于兼容常量 ``_SYSTEM_PROMPT``。

  既有测试 ``test_stock_analyzer_index.py`` 就断言这一条; 这里再钉一次, 让
  「加 market 维不许动 A 股提示词」成为本文件自己的契约。
  """
  assert sa._build_system_prompt(False) == sa._SYSTEM_PROMPT
  assert sa._build_system_prompt(True, Market.CN) == sa._build_system_prompt(True)


def test_cn_persona_keeps_original_spacing():
  """人设里是「15 年 A 股一线研究经验」—— 中英混排的空格必须保留。"""
  assert "15 年 A 股一线研究经验" in sa._SYSTEM_PROMPT
  assert mg.prompt_market_label(Market.CN) == " A 股"


def test_hk_persona_has_no_stray_space():
  """港股是纯中文称谓, 不能渲染成「15 年 港股一线」。

  空格住在**值**里而不是模板里, 就是为了让两种排印同时正确。
  """
  assert mg.prompt_market_label(Market.HK) == "港股"
  assert "15 年港股一线研究经验" in sa._build_system_prompt(False, Market.HK)
  assert "15 年 港股" not in sa._build_system_prompt(False, Market.HK)


# ===== 2. 港股护栏 6 条 =====

def test_hk_guardrail_injected_into_system_prompt():
  hk = sa._build_system_prompt(False, Market.HK)
  assert "港股市场口径护栏" in hk
  assert mg.guardrail_prompt(Market.HK) in hk
  # 护栏在 TAIL 之后、输出契约 (数字纪律 + 答题卡) 之前: 后两者必须留在最贴近
  # 用户消息的位置, 护栏属于领域口径, 与分析准则同层。
  from app.services.ai_number_gate import NUMBER_RULE_PROMPT
  from app.services.ai_verdict import VERDICT_PROMPT
  assert hk.index("港股市场口径护栏") < hk.index(NUMBER_RULE_PROMPT)
  assert hk.index(NUMBER_RULE_PROMPT) < hk.index(VERDICT_PROMPT)


@pytest.mark.parametrize("fragment", [
  # ① 禁止套用 A 股规则
  "禁止套用 A 股规则", "T+1 回转交易", "ST/*ST", "过户费",
  # ② symbol 口径: 5 位 (本项目口径, 不是 QuantMind 的 4 位)
  "5 位数字", "00700.HK", "创业板 (8 开头) 同样保留 5 位",
  # ③ 交易机制
  "T+0", "T+2 交收不影响交易", "board lot", "不要假设一手 100 股",
  # ④ 费用 / 货币 / 基准
  "佣金约 0.03%", "印花税 0.1%", "卖出单边",
  # ⑤ 数据口径
  "分钟延时", "CURRENCY", "估值比率需注意分子分母的货币口径",
  # ⑥ null ≠ 没有
  "consecutive_limit_ups", "signal_limit_up", "signal_broken_limit_up",
  "港股无此语义", "不是「该股没涨停」",
])
def test_hk_guardrail_contains_all_six_rules(fragment):
  """方案 §9.1 的 6 条逐条落地。第 ⑥ 条是「AI 版的看起来对但错」。"""
  assert fragment in mg.guardrail_prompt(Market.HK)


def test_hk_guardrail_forbids_limit_up_inference():
  """第 ⑥ 条必须**显式禁止**把 null 连板数读成「没涨停」。

  这是护栏存在的核心理由: ``kline_sync_hk`` 明确写了港股
  ``consecutive_limit_ups`` 恒 0「是正确结果不是缺数据」, 模型却会用 A 股常识
  脑补出「无连板情绪 / 打板资金缺席」。
  """
  text = mg.guardrail_prompt(Market.HK)
  for banned in ("未涨停", "无连板情绪", "打板资金缺席", "情绪到位"):
    assert banned in text, f"护栏应显式禁止推断「{banned}」"


def test_hk_guardrail_overrides_ashare_wording_elsewhere():
  """护栏必须声明优先级高于上文, 否则与第 5 节的「涨停/连板/炸板」示例自相矛盾。"""
  text = mg.guardrail_prompt(Market.HK)
  assert "优先级高于上文任何章节" in text
  assert "无此语义" in text


# ===== 3. 数值读规则表, 不写死 (方案 §9.1 明确要求) =====

def _fake_rules(**overrides):
  """返回一个把 ``rules_for`` 换成「改写过的 HK_RULES」的补丁函数。"""
  modified = dataclasses.replace(HK_RULES, **overrides)

  def _patch(monkeypatch):
    monkeypatch.setattr(mg, "rules_for", lambda _market=None: modified)

  return _patch


def test_delay_minutes_come_from_rules_not_hardcoded(monkeypatch):
  """延时分钟数读 ``rules_for(market).quote_delay_minutes``, 不是写死的 15。"""
  _fake_rules(quote_delay_minutes=99)(monkeypatch)
  text = mg.guardrail_prompt(Market.HK)
  assert "99 分钟延时" in text
  assert "15 分钟延时" not in text


def test_currency_and_benchmark_come_from_rules(monkeypatch):
  _fake_rules(currency="USD", default_benchmark="SPX")(monkeypatch)
  text = mg.guardrail_prompt(Market.HK)
  assert "USD" in text and "SPX" in text
  assert "HKD" not in text and "HSI" not in text


def test_lot_size_wording_follows_rules(monkeypatch):
  """``lot_size=None`` → 要求查个股真值; 有值 → 直接写出该值。

  🔴 HK_RULES.lot_size 刻意是 None (消费方必须查 ``instruments_hk.lot_size``),
  所以护栏不能写「一手 100 股」, 也不能写死任何数字。
  """
  assert HK_RULES.lot_size is None
  assert "board lot" in mg.guardrail_prompt(Market.HK)

  _fake_rules(lot_size=400)(monkeypatch)
  text = mg.guardrail_prompt(Market.HK)
  assert "固定 400 股" in text
  assert "board lot" not in text


def test_turnover_and_price_limit_wording_follow_rules(monkeypatch):
  """T+0/T+1 与有无涨跌停由规则表推导, 不是文案里写死的字。"""
  text = mg.guardrail_prompt(Market.HK)
  assert "T+0" in text and "无涨跌停" in text

  _fake_rules(t_plus_1=True, has_price_limit=True)(monkeypatch)
  flipped = mg.guardrail_prompt(Market.HK)
  assert "T+1" in flipped and "有涨跌停" in flipped
  assert "T+0" not in flipped


def test_real_hk_rules_values_appear_in_guardrail():
  """正向核对: 当前 HK_RULES 的真值确实出现在护栏里 (防止规则表改了文案没跟)。"""
  text = mg.guardrail_prompt(Market.HK)
  assert f"{HK_RULES.quote_delay_minutes} 分钟延时" in text
  assert HK_RULES.currency in text
  assert HK_RULES.default_benchmark in text


# ===== 4. 注册表完整性与 fail-closed =====

def test_guardrail_registry_covers_every_market():
  """新增市场必须注册护栏 —— 漏了当场 ValueError, 不静默按 A 股 (空串) 处理。

  静默返回空串意味着新市场拿到一份**没有纠偏**的 A 股提示词, 比报错危险得多。
  """
  assert set(mg._GUARDRAILS) == set(Market)
  assert set(mg._PROMPT_LABEL) == set(Market)
  assert set(mg._FINANCIAL_MISSING_NOTE) == set(Market)


def test_unregistered_market_raises(monkeypatch):
  monkeypatch.setitem(mg._GUARDRAILS, Market.HK, None)
  monkeypatch.setattr(
    mg, "_GUARDRAILS",
    {k: v for k, v in mg._GUARDRAILS.items() if v is not None},
  )
  with pytest.raises(ValueError, match="未注册 AI 提示词护栏"):
    mg.guardrail_prompt(Market.HK)


def test_registry_values_are_callables_not_frozen_strings():
  """🔴 注册表存**函数**而不是算好的字符串。

  存字符串 = 在 import 时就把 ``rules_for`` 的结果冻住, 之后规则表被改 / 被
  monkeypatch 都不会反映到提示词里 —— 与 ``stock_analyzer._TODAY_BY_MARKET``、
  ``ai_data_quality._CALENDAR_SOURCES`` 同一个 import 期绑定陷阱。
  """
  for market, builder in mg._GUARDRAILS.items():
    assert callable(builder), f"{market} 的护栏必须是可调用对象, 不能是冻结字符串"
    assert isinstance(builder(), str)


def test_guardrail_reads_rules_at_call_time(monkeypatch):
  """同一条: 从行为面证明 ``rules_for`` 是**调用时**读的。"""
  before = mg.guardrail_prompt(Market.HK)
  _fake_rules(quote_delay_minutes=42)(monkeypatch)
  after = mg.guardrail_prompt(Market.HK)
  assert before != after
  assert "42 分钟延时" in after


def test_string_market_alias_normalized():
  """``"hk"`` / ``"HK"`` / ``Market.HK`` 三种写法拿到同一份护栏。"""
  expected = mg.guardrail_prompt(Market.HK)
  assert expected
  assert mg.guardrail_prompt("hk") == expected
  assert mg.guardrail_prompt("HK") == expected
  assert mg.guardrail_prompt("Hk") == expected


# ===== 5. 财务缺失文案分市场 =====

def test_cn_financial_note_keeps_free_mode_copy():
  note = mg.financial_missing_note(Market.CN)
  assert note.startswith(f"({FIN_TRIGGER}:")
  assert "Free 模式或尚未同步财务报表" in note


def test_hk_financial_note_corrects_the_wrong_advice():
  """🔴 港股不能沿用「Free 模式或尚未同步」—— 那是**错误建议**。

  用户照着去开 Free 模式 / 点同步永远修不好, 真实原因是港股财报口径与报告货币
  不同, 需要单独接入 (PR5)。
  """
  note = mg.financial_missing_note(Market.HK)
  assert note.startswith(f"({FIN_TRIGGER}:")
  assert "港股" in note
  assert "不是 Free 模式限制" in note
  # 允许出现「Free 模式」只为了**否定**它, 不能出现 A 股那句原样建议
  assert "当前为 Free 模式或尚未同步财务报表" not in note


def test_both_notes_keep_the_section4_trigger_phrase():
  """系统提示词第 4 节靠 ``该标的暂无财务数据`` 原文匹配。

  改了措辞 → 第 4 节静默不触发 → 模型在无财务数据时**编造 ROE / 营收**。
  这条测试是那个隐性耦合的钉子。
  """
  assert FIN_TRIGGER in sa._SYSTEM_PROMPT  # 第 4 节确实写了这个触发短语
  for market in Market:
    assert FIN_TRIGGER in mg.financial_missing_note(market)


# ===== 6. _load_financials 按市场分派 =====

def test_hk_financials_return_empty_without_scanning_ashare_tables(monkeypatch, tmp_path):
  """🔴 港股不去扫 A 股财务宽表。

  旧实现对港股也调 ``get_financial_df("metrics"/"income")``: 港股代码在 A 股宽表里
  一行都没有, 白扫全表拿到空帧, 再据此输出错误的「Free 模式」文案。
  """
  calls: list[str] = []

  def _boom(_data_dir, table):
    calls.append(table)
    raise AssertionError("港股不应读 A 股财务表")

  monkeypatch.setattr(sa, "get_financial_df", _boom)
  assert sa._load_financials(tmp_path, HK) == {}
  assert calls == []


def test_cn_financials_still_scan_both_tables(monkeypatch, tmp_path):
  """A 股行为逐字保留: 照旧扫 metrics + income 两张表。"""
  import polars as pl

  calls: list[str] = []

  def _fake(_data_dir, table):
    calls.append(table)
    return pl.DataFrame()

  monkeypatch.setattr(sa, "get_financial_df", _fake)
  assert sa._load_financials(tmp_path, CN) == {"metrics": [], "income": []}
  assert calls == ["metrics", "income"]


def test_financial_loader_registry_covers_every_market():
  assert set(sa._FINANCIAL_LOADERS) == set(Market)


def test_financial_loader_unregistered_market_raises(monkeypatch, tmp_path):
  monkeypatch.setattr(sa, "infer_market", lambda _s: Market.HK)
  monkeypatch.setattr(
    sa, "_FINANCIAL_LOADERS",
    {k: v for k, v in sa._FINANCIAL_LOADERS.items() if k is not Market.HK},
  )
  with pytest.raises(ValueError, match="未注册 AI 财务读取器"):
    sa._load_financials(tmp_path, HK)


def test_load_financials_signature_stays_two_args():
  """签名**刻意**保持 ``(data_dir, symbol)`` 两参数。

  ``ai_audit.py`` 与 ``tests/test_ai_audit_ledger.py`` 都以两参数调用 / 打桩
  (``lambda data_dir, symbol: payload``); 加第三个位置参数会让那些 monkeypatch
  静默失配。市场由 symbol 唯一决定, 无需外部传入。
  """
  import inspect

  params = list(inspect.signature(sa._load_financials).parameters)
  assert params == ["data_dir", "symbol"]


# ===== 7. _build_user_prompt 的 market 维 =====

def _user_prompt(symbol: str, market, asset_type: str = "stock") -> str:
  return sa._build_user_prompt(
    [], {}, {}, 10.0, symbol, "", asset_type=asset_type, market=market,
  )


def test_user_prompt_defaults_to_cn_copy():
  """不传 market → A 股文案 (既有调用方零改动)。"""
  out = sa._build_user_prompt([], {}, {}, 10.0, CN, "")
  assert "Free 模式或尚未同步财务报表" in out
  assert out == _user_prompt(CN, Market.CN)


def test_user_prompt_hk_copy():
  out = _user_prompt(HK, Market.HK)
  assert "港股" in out
  assert "不是 Free 模式限制" in out
  assert "当前为 Free 模式或尚未同步财务报表" not in out


def test_market_param_added_at_end_of_signature():
  """``market`` 必须加在参数表**末尾**, 否则动到既有位置参数调用方。"""
  import inspect

  params = list(inspect.signature(sa._build_user_prompt).parameters)
  assert params[-1] == "market"
  assert inspect.signature(sa._build_user_prompt).parameters["market"].default is Market.CN


@pytest.mark.parametrize("asset_type", ["etf", "index"])
def test_asset_type_branches_win_over_market_note(asset_type):
  """ETF / 指数有自己的常态文案, 与 market 维**正交** (不走财务缺失兜底)。"""
  for market in Market:
    out = _user_prompt("510300.SH", market, asset_type=asset_type)
    assert FIN_TRIGGER not in out
    assert "Free 模式" not in out
