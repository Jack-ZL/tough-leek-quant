"""AI 分析 prompt 指数 / ETF 文案测试。"""
from app.services.stock_analyzer import _build_user_prompt


def test_user_prompt_index_no_financials():
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 3000.0}],
        fins={"metrics": [], "income": []},
        levels={}, close=3000.0, symbol="000001.SH", focus="",
        asset_type="index",
    )
    assert "指数" in prompt
    assert "Free 模式" not in prompt  # 指数无财务是常态, 不走 Free 文案


def test_user_prompt_etf_no_financials():
    """ETF 无上市公司财务是常态, 不得走股票 Free/未同步文案。

    指数分支已单独处理, ETF 若落入 else, 提示词会写成「Free 模式或尚未
    同步财务报表」, 模型按系统提示词第 4 节输出「财务接入中」, 用户会以为
    是套餐或同步问题, 而不是场内基金本来就没有公司报表。
    """
    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 4.12}],
        fins={"metrics": [], "income": []},
        levels={}, close=4.12, symbol="510300.SH", focus="",
        asset_type="etf",
    )
    assert "ETF" in prompt
    assert "Free 模式" not in prompt
    assert "尚未同步财务报表" not in prompt


# ===== V3: 虚拟持仓注入 =====

def test_user_prompt_injects_paper_position():
    """模拟盘持有该标的: 注入持仓数量/成本/浮盈, 并要求 AI 给持仓建议。"""
    from app.services.stock_analyzer import _paper_position_part, _build_user_prompt

    part = _paper_position_part([{"account_id": "default", "qty": 1000, "avg_cost": 10.0}],
                                close=11.0, raw_close=11.0)
    assert "1000 股" in part
    assert "10.000" in part
    assert "+10.00%" in part
    assert "虚拟持仓" in part

    prompt = _build_user_prompt(
        kline_tail=[{"date": "2026-07-24", "close": 11.0}],
        fins={"metrics": [], "income": []},
        levels={}, close=11.0, symbol="600000.SH", focus="",
        paper_positions=[{"account_id": "default", "qty": 1000, "avg_cost": 10.0}],
        raw_close=11.0,
    )
    assert "模拟盘" in prompt and "持仓 1000 股" in prompt


def test_paper_position_part_multi_account():
    """V2 多账户: 逐账户注入, 空仓账户跳过, 全空返回空串。"""
    from app.services.stock_analyzer import _paper_position_part

    part = _paper_position_part(
        [
            {"account_id": "a1", "qty": 100, "avg_cost": 10.0},
            {"account_id": "a2", "qty": 0, "avg_cost": 10.0},
            {"account_id": "a3", "qty": 200, "avg_cost": 20.0},
        ],
        close=11.0, raw_close=11.0,
    )
    assert "账户 a1" in part and "账户 a3" in part
    assert "a2" not in part


def test_paper_position_part_empty_without_position():
    """无持仓/零仓/无效成本: 返回空串, 不注入误导性「空仓」表述。"""
    from app.services.stock_analyzer import _paper_position_part

    assert _paper_position_part([], 11.0, 11.0) == ""
    assert _paper_position_part([{"qty": 0, "avg_cost": 10.0}], 11.0, 11.0) == ""
    assert _paper_position_part([{"qty": 100, "avg_cost": 0}], 11.0, 11.0) == ""


def test_paper_position_part_prefers_raw_close():
    """除权后前复权 close 与 raw 成本混算会产生虚假盈亏: 必须优先 raw_close。

    raw 成本 20 元, 除权后前复权 close=11、raw_close=21: 用 close 算出 -45% 是错的。
    """
    from app.services.stock_analyzer import _paper_position_part

    part = _paper_position_part([{"qty": 100, "avg_cost": 20.0}], close=11.0, raw_close=21.0)
    assert "+5.00%" in part
    assert "-45" not in part


# ===== 联网搜索: 系统提示词第 5 节双变体 =====

def test_system_prompt_search_variant():
    from app.services.stock_analyzer import _build_system_prompt

    online = _build_system_prompt(True)
    assert "消息面(联网搜索)" in online
    # 检索类别引导 (公司4类+行业相关)
    assert "检索范围" in online
    assert "经营/订单" in online and "风险事件" in online
    assert "股东行为" in online and "资本运作" in online
    # 防幻觉/客观性关键约束
    assert "未检索到相关消息" in online
    assert "[消息]" in online and "[推断]" in online
    assert "交叉验证" in online
    # 头尾结构完整 (第 6 节与免责仍在)
    assert "综合研判与风险提示" in online
    assert "本内容由 AI" in online
    # 不得残留无联网变体文案
    assert "价量异动推断)" not in online


def test_system_prompt_offline_variant():
    from app.services.stock_analyzer import _SYSTEM_PROMPT, _build_system_prompt

    offline = _build_system_prompt(False)
    assert offline == _SYSTEM_PROMPT  # 兼容旧引用: 默认无联网
    assert "消息面(价量异动推断)" in offline
    assert "当前模型未启用联网搜索" in offline
    assert "综合研判与风险提示" in offline
    # 不得声称有搜索能力
    assert "你已启用联网搜索" not in offline


def test_user_prompt_news_hint_follows_web_search():
    """指数/ETF 无财务文案里的消息面指引须与联网状态一致。"""
    common = dict(
        kline_tail=[{"date": "2026-07-24", "close": 3000.0}],
        fins={"metrics": [], "income": []},
        levels={}, close=3000.0, symbol="000001.SH", focus="",
        asset_type="index",
    )
    offline = _build_user_prompt(**common)
    assert "消息面维度基于价量异动推断即可" in offline

    online = _build_user_prompt(**common, web_search=True)
    assert "联网检索" in online
    assert "价量异动推断即可" not in online
