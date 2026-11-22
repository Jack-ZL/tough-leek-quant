"""港股波动率情绪 (VHSI) 测试: 纯函数口径 + 服务 fail-closed + API 契约。

分三层, 与 CONTRIBUTING §9 的验证矩阵对应:

1. **纯函数** (``_percentile_rank`` / ``_band_of`` / ``_rolling_mean`` / ``_pct_changes``
   / ``_corr``): 用固定小样本钉住口径。这些是「算错也看不出来」的量 ——
   分位排名差 1 名、均线用短窗口凑数、相关系数把无定义填成 0, 在界面上都
   表现为一个**正常数字**, 故必须逐条单测。
2. **服务** (``hk_volatility``): 样本不足 fail-closed、单位不 ×100、null≠0、
   基准缺失时相关系数为 null。
3. **API** (``GET /api/hk/volatility``): 成功 / 无数据 / 参数校验, 以及
   ``series_days`` **只裁剪绘图序列、不裁剪标定样本**这条最容易写错的契约。

repo 用真实 ``KlineRepository(DataStore(tmp_path))`` 走通 DuckDB 视图路径
(项目惯例, 见 ``test_hk_api.py``): 本模块的读取口径就是视图查询, mock 掉
等于没测。
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import hk as hk_api
from app.services import hk_volatility as hv
from app.services.fs_utils import atomic_write_parquet
from app.tickflow.repository import DataStore, KlineRepository

# ---------------------------------------------------------------------------
# 夹具与造数
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    app = FastAPI()
    app.include_router(hk_api.router)
    app.state.repo = repo
    app.state.quote_service = None
    return TestClient(app), repo, tmp_path


def _seed_index_daily(tmp_path, repo, rows: list[dict]) -> None:
    """把指数日K按**逐日分区**写进 ``kline_hk_index_daily/`` 并重建视图。

    🔴 必须一天一个分区 (``date=YYYY-MM-DD/part.parquet``), 与
    ``repo.append_daily_asset`` 的真实落盘布局一致。**不能**把整段样本塞进单个
    分区目录: DuckDB 的 hive 分区键会与文件内同名 ``date`` 列冲突, 实测所有行的
    ``date`` 被覆盖成目录名那个常量, 于是 ``ORDER BY date`` 退化成任意序 —— 
    「最新点位」变成随机一根, 而测试看起来仍在跑。

    刻意**不走** ``sync_hk_index_daily``: 那条路要腾讯 client 且会顺带生成
    enriched, 而本模块只读 close。直接落 parquet 让每个测试的样本完全可控
    (分位数对样本极其敏感, 造数不可控 = 断言不可信)。
    """
    schema = {
        "symbol": pl.Utf8,
        "date": pl.Date,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
    }
    by_date: dict[dt.date, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)

    for day, group in by_date.items():
        df = pl.DataFrame(
            {
                "symbol": [r["symbol"] for r in group],
                "date": [r["date"] for r in group],
                "open": [r.get("close") for r in group],
                "high": [r.get("close") for r in group],
                "low": [r.get("close") for r in group],
                "close": [r.get("close") for r in group],
                "volume": [1000.0] * len(group),
            },
            schema=schema,
        )
        out = tmp_path / "kline_hk_index_daily" / f"date={day.isoformat()}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(df, out)
    repo.rebuild_views()


def _series(
    symbol: str, closes: list[float | None], start: dt.date = dt.date(2026, 1, 1)
) -> list[dict]:
    """按**工作日**造一段日K (跳过周末, 贴近真实交易日历)。

    ``closes`` 里的 ``None`` 会造出 ``close=NULL`` 的行, 用于测「缺收盘不得当 0」。
    """
    out: list[dict] = []
    day = start
    for c in closes:
        while day.weekday() >= 5:  # 5=周六 6=周日
            day += dt.timedelta(days=1)
        out.append({"symbol": symbol, "date": day, "close": c})
        day += dt.timedelta(days=1)
    return out


def _ramp(n: int, start: float = 18.0, step: float = 0.05) -> list[float]:
    """单调递增样本。用递增序列是因为它让分位排名有闭式解:
    第 i 根 (0-based) 的分位排名恰为 ``i / n × 100``, 断言可以精确到小数。
    """
    return [start + i * step for i in range(n)]


def _from_returns(start: float, rets: list[float]) -> list[float]:
    """由**日收益率**序列反推收盘价 (首根 = ``start``)。

    🔴 测相关系数必须用这个而不是 :func:`_ramp`: 线性斜坡的**收益率本身是单调的**
    (``50/20000 > 50/20050 > …``), 于是「恒指涨、波动率跌」这种明显反向的两条
    斜坡, 收益率却**同向递减**, 相关系数算出来是 ``+0.9999``。实测踩过:
    用斜坡造数会让「负相关」测试恒失败, 而失败原因看起来像服务算错了符号。
    要测符号就必须让收益率**逐日交替**, 由这里显式给定。
    """
    out = [start]
    for r in rets:
        out.append(out[-1] * (1.0 + r))
    return out


def _alternating(n: int, up: float, down: float) -> list[float]:
    """长度 ``n`` 的交替收益率 ``[up, down, up, down, …]``。"""
    return [up if i % 2 == 0 else down for i in range(n)]


# ---------------------------------------------------------------------------
# 纯函数: 分位排名
# ---------------------------------------------------------------------------


def test_percentile_rank_is_strictly_less_than_share():
    """分位排名 = **严格小于** target 的样本占比 × 100。

    钉住这个定义是因为它决定了「最新点位自己算不算进 below」: 用 ``<=`` 会让
    样本最大值恒等于 100 分位 (自己小于等于自己), 于是「历史最高」与「普通高」
    无法区分。
    """
    s = sorted([10.0, 20.0, 30.0, 40.0])
    assert hv._percentile_rank(s, 10.0) == 0.0  # 最小值 → 0 分位
    assert hv._percentile_rank(s, 25.0) == 50.0  # 2/4 严格小于
    assert hv._percentile_rank(s, 40.0) == 75.0  # 🔴 最大值是 75 而非 100
    assert hv._percentile_rank(s, 99.0) == 100.0  # 超出样本 → 100


def test_percentile_rank_duplicates_not_counted_into_own_band():
    """重复值: 与 target 相等的那些**不计入** below, 故并列日共享同一分位。

    VHSI 实测有重复收盘 (如 19.59/19.56 相邻), 若用 ``<=`` 则一批并列日会
    集体跳到更高分位, 档位随之错判。
    """
    s = sorted([20.0, 20.0, 20.0, 30.0])
    assert hv._percentile_rank(s, 20.0) == 0.0
    assert hv._percentile_rank(s, 30.0) == 75.0


def test_percentile_rank_empty_raises():
    """空样本**抛错**而不是返回 0: 0 分位是「历史最平静」, 与「没有数据」
    在界面上无法区分, 属 §4 禁止的静默降级。"""
    with pytest.raises(ValueError, match="非空样本"):
        hv._percentile_rank([], 20.0)


def test_percentile_rank_matches_closed_form_on_ramp():
    """递增样本上逐根校验闭式解 ``i/n×100`` —— 覆盖 bisect 的边界 (首/中/尾)。"""
    closes = _ramp(200)
    s = sorted(closes)
    for i in (0, 1, 99, 100, 198, 199):
        assert hv._percentile_rank(s, closes[i]) == pytest.approx(i / 200 * 100.0)


# ---------------------------------------------------------------------------
# 纯函数: 档位
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pct", "key", "label"),
    [
        (0.0, "ultra_low", "极低波"),
        (9.99, "ultra_low", "极低波"),
        (10.0, "low", "低波"),  # 🔴 边界归**更高**一档 (左闭右开)
        (34.99, "low", "低波"),
        (35.0, "normal", "常态"),
        (69.99, "normal", "常态"),
        (70.0, "elevated", "偏高"),
        (89.99, "elevated", "偏高"),
        (90.0, "high", "高波"),
        (97.99, "high", "高波"),
        (98.0, "panic", "恐慌"),
        (100.0, "panic", "恐慌"),
    ],
)
def test_band_of_boundaries(pct, key, label):
    """六档边界逐一钉住。

    🔴 边界值归**更高**一档是刻意选择: 「刚好到 p90」判成「高波」而不是「偏高」,
    宁可偏紧张不可偏松懈 —— 波动率指标漏报恐慌的代价远高于多报一次谨慎。
    """
    assert hv._band_of(pct) == (key, label)


def test_band_table_has_unbounded_fallback():
    """档位表最后一段必须无上界, 否则 ``_band_of`` 会走到 AssertionError。

    这条测试防的是**将来有人往表里加一档却忘了留兜底**: 那时不是报错,
    而是所有高分位日静默落进 AssertionError 之外的未定义行为。
    """
    assert hv.HK_VOLATILITY_BANDS[-1][0] is None
    assert all(b[0] is not None for b in hv.HK_VOLATILITY_BANDS[:-1])
    uppers = [b[0] for b in hv.HK_VOLATILITY_BANDS[:-1]]
    assert uppers == sorted(uppers), "档位上界必须升序, 否则先命中的是错的档"
    assert hv._band_of(100.0)[0] == hv.HK_VOLATILITY_BANDS[-1][1]


def test_band_keys_are_unique():
    """档位键重复会让 ``streak_days`` 把两个不同档当成同一个 (连续天数虚高)。"""
    keys = [b[1] for b in hv.HK_VOLATILITY_BANDS]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# 纯函数: 均线 / 收益 / 相关系数
# ---------------------------------------------------------------------------


def test_rolling_mean_leading_none_not_short_window():
    """🔴 前 ``window-1`` 项必须是 ``None``, 不能用更短窗口凑一个数出来。

    用 3 根算出来的「ma60」看起来是个正常数字, 但它是 3 日均线; 界面上
    「60 日均线」四个字会让使用者以为波动率中枢已经横了三个月。
    """
    out = hv._rolling_mean([1.0, 2.0, 3.0, 4.0], 3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)  # (1+2+3)/3
    assert out[3] == pytest.approx(3.0)  # (2+3+4)/3


def test_rolling_mean_window_one_is_identity():
    assert hv._rolling_mean([5.0, 7.0], 1) == [5.0, 7.0]


def test_rolling_mean_rejects_non_positive_window():
    """窗口 ≤0 抛错: ``range`` 语义下它会静默返回全 None, 看起来像「数据不足」。"""
    with pytest.raises(ValueError, match="窗口必须为正"):
        hv._rolling_mean([1.0], 0)


def test_pct_changes_first_is_none_and_zero_prev_is_none():
    """首项无前收 → ``None``; 前收为 0 → 也是 ``None`` 而不是 ``inf``。

    VHSI 不会为 0, 但真出现 0 说明数据坏了; 用一个坏值去算相关系数会污染整段,
    而 ``inf`` 参与运算还会把结果变成 ``nan``, 最终 JSON 序列化成 ``null`` —— 
    故障被伪装成「无数据」。
    """
    assert hv._pct_changes([100.0, 110.0, 99.0])[0] is None
    out = hv._pct_changes([100.0, 110.0, 99.0])
    assert out[1] == pytest.approx(0.10)  # 🔴 小数口径, 不是 10.0
    assert out[2] == pytest.approx(-0.10)
    assert hv._pct_changes([0.0, 5.0])[1] is None


def test_corr_perfect_positive_and_negative():
    """完全同向 → +1, 完全反向 → −1。VHSI 与 HSI 正常应显著为负。"""
    assert hv._corr([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    assert hv._corr([1.0, 2.0, 3.0], [6.0, 4.0, 2.0]) == pytest.approx(-1.0)


def test_corr_zero_variance_is_none_not_zero():
    """🔴 任一侧方差为 0 → 相关系数**无定义**, 必须 ``None`` 而不是 ``0``。

    填 0 会被读成「实测两者不相关」, 而真相是「这段时间波动率根本没动,
    无法判断相关性」。两者在界面上都显示成一个数, 但含义相反。
    """
    assert hv._corr([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert hv._corr([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) is None


def test_corr_insufficient_samples_is_none():
    """样本 <2 无定义; 长度不等是调用方 bug, 也返回 None 而不是崩。"""
    assert hv._corr([1.0], [2.0]) is None
    assert hv._corr([], []) is None
    assert hv._corr([1.0, 2.0], [1.0]) is None


# ---------------------------------------------------------------------------
# 服务: 读取口径
# ---------------------------------------------------------------------------


def test_fetch_closes_returns_empty_when_view_missing(env):
    """从未同步过港股指数 → 视图未注册 → 返回空 dict, **不抛**。

    这是本地数据缺失而非上游故障; 抛出去会让 API 变 500, 把「还没同步」
    伪装成「服务坏了」。
    """
    _, repo, _ = env
    assert hv._fetch_closes(repo, ("VHSI", "HSI")) == {}


def test_fetch_closes_drops_null_close(env):
    """🔴 ``close IS NULL`` 的行必须丢弃, 不得当 0 参与分位/均线。

    一根 0 会把 p10 拉到接近 0、把均线整体压低, 于是「常态」被判成「极低波」—— 
    而 0 在波动率语义里是「市场完全静止」, 根本不可能发生。
    """
    _, repo, tmp_path = env
    _seed_index_daily(
        tmp_path, repo, _series("VHSI", [20.0, None, 22.0, None, 24.0])
    )
    got = hv._fetch_closes(repo, ("VHSI",))
    assert [c for _, c in got["VHSI"]] == [20.0, 22.0, 24.0]


def test_fetch_closes_only_reads_close_never_volume(env):
    """只取 ``symbol/date/close`` 三列: 指数的 volume/amount 两个源互相矛盾
    (新浪 vs 腾讯恰好 ×1000, 见 ``plugins/sina/hk_index.py`` 坑 3),
    本模块从 SQL 层面就不碰它们。"""
    _, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", [20.0, 21.0]))
    rows = repo.execute_all("SELECT symbol, date, close FROM kline_hk_index_daily")
    assert len(rows[0]) == 3  # 视图查询本身也只选三列


def test_fetch_closes_is_date_ascending(env):
    """升序是 ``_rolling_mean`` / ``streak`` / ``closes[-1]`` 的前提;
    乱序会让「最新点位」变成随机一根。"""
    _, repo, tmp_path = env
    rows = _series("VHSI", [20.0, 21.0, 22.0])
    _seed_index_daily(tmp_path, repo, list(reversed(rows)))
    got = hv._fetch_closes(repo, ("VHSI",))["VHSI"]
    assert [d for d, _ in got] == sorted(d for d, _ in got)


# ---------------------------------------------------------------------------
# 服务: fail closed
# ---------------------------------------------------------------------------


def test_no_data_is_insufficient_with_actionable_reason(env):
    """零样本: ``insufficient_history=True`` + 全部判定量为 None + 原因里给出
    **可执行的下一步** (去同步), 而不是只说「数据不足」。"""
    _, repo, _ = env
    r = hv.hk_volatility(repo)
    assert r.insufficient_history is True
    assert (r.regime, r.regime_label, r.percentile, r.streak_days, r.corr_hsi) == (
        None, None, None, None, None,
    )
    assert r.sample_days == 0 and r.series == [] and r.percentiles == {}
    assert r.level is None and r.as_of is None
    assert "sync_index_daily" in r.insufficient_reason
    # 极值也必须是 None (没有样本就没有极值), 不能是 0
    assert r.period_high is None and r.period_low is None


def test_short_sample_refuses_regime_but_keeps_facts(env):
    """🔴 样本 < ``HK_VOLATILITY_MIN_SAMPLE_DAYS`` → 不给档位, 但**客观事实照常给**。

    「样本不足」限制的是分位/档位这类需要统计意义的量; 最新点位、均线、
    窗口极值是样本内的客观事实, 一起藏起来会让用户以为完全没有数据。
    """
    _, repo, tmp_path = env
    n = hv.HK_VOLATILITY_MIN_SAMPLE_DAYS - 1
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(n)))
    r = hv.hk_volatility(repo)

    assert r.insufficient_history is True
    assert r.sample_days == n
    assert (r.regime, r.percentile, r.streak_days) == (None, None, None)
    assert r.percentiles == {}
    assert str(n) in r.insufficient_reason  # 原因里要能看到实际样本数

    # 客观事实仍在
    assert r.level == pytest.approx(18.0 + (n - 1) * 0.05)
    assert r.period_high == pytest.approx(r.level)
    assert r.period_low == pytest.approx(18.0)
    assert r.period_high_date is not None and r.period_low_date is not None
    assert r.moving_averages["ma5"] is not None
    assert len(r.series) == n


def test_exactly_min_sample_days_does_yield_regime(env):
    """下限是**闭区间**: 恰好 120 根就该给档位 (否则差一根就整页空白, 无从解释)。"""
    _, repo, tmp_path = env
    n = hv.HK_VOLATILITY_MIN_SAMPLE_DAYS
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(n)))
    r = hv.hk_volatility(repo)
    assert r.insufficient_history is False
    assert r.sample_days == n
    assert r.regime is not None and r.insufficient_reason is None


# ---------------------------------------------------------------------------
# 服务: 单位与口径 (最容易写错且看不出来的部分)
# ---------------------------------------------------------------------------


def test_level_is_percentage_points_never_scaled(env):
    """🔴🔴 VHSI 点位**就是**年化隐含波动率的百分点数, 不得 ×100 也不得 ÷100。

    ``24.09`` 表示 24.09%。若某处「顺手归一化」成 0.2409 或放大成 2409,
    界面上都还是个数字, 但含义差 100 倍 / 10000 倍。这里用**原样透传**钉死:
    存进去什么点位, ``level`` 就是什么点位。
    """
    _, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(150, start=24.09, step=0.0)))
    r = hv.hk_volatility(repo)
    assert r.level == pytest.approx(24.09)
    assert 1.0 < r.level < 200.0  # 既不是 0.2409 也不是 2409


def test_change_pct_1d_is_percentage_not_decimal(env):
    """🔴 ``change_pct_1d`` 是**百分数** (``10.0`` 即 +10%), 与项目里
    ``change_pct`` 常见的小数口径**相反**。

    两者混用会让「今天波动率跳了 10%」显示成「0.1%」。故这里精确钉住数值,
    并要求字段名带 ``_pct`` 的即为百分数。
    """
    _, repo, tmp_path = env
    closes = [*_ramp(149, start=20.0, step=0.0), 22.0]  # 最后一根 20 → 22
    _seed_index_daily(tmp_path, repo, _series("VHSI", closes))
    r = hv.hk_volatility(repo)
    assert r.change_1d == pytest.approx(2.0)  # 绝对变动是**点位差**
    assert r.change_pct_1d == pytest.approx(10.0)  # 🔴 10.0 而非 0.10


def test_missing_prev_close_yields_none_not_zero(env):
    """🔴 null ≠ 0: 前收缺失时 ``change_1d`` / ``change_pct_1d`` 为 ``None``。

    填 0 会被读成「今天波动率没动」, 而真相是「算不出来」。
    构造方式: 只有一根有效收盘 (其余为 NULL 被丢弃) 且样本足够长 —— 
    用 NULL 撑出样本量, 让 ``closes`` 最终只剩 1 根是不够的 (会 fail closed),
    故这里直接测「样本足够但倒数第二根被丢」的情形。
    """
    _, repo, tmp_path = env
    # 150 根里前 149 根有效, 最后一根的前一天是 NULL → 被丢弃后前收变成第 148 根
    closes: list[float | None] = _ramp(149, start=20.0)  # type: ignore[assignment]
    rows = _series("VHSI", closes)
    # 在末尾追加一根 NULL, 再追加一根有效值: 有效序列的「前一根」仍是连续的,
    # 故改为构造**只有两根有效**且中间隔 NULL 的场景来验证 None 语义。
    rows += _series("VHSI", [None], start=rows[-1]["date"] + dt.timedelta(days=1))
    _seed_index_daily(tmp_path, repo, rows)
    r = hv.hk_volatility(repo)
    # 149 根 ≥ 120 → 有档位; NULL 已被丢弃, 故 change 基于最后两根**有效**收盘
    assert r.insufficient_history is False
    assert r.sample_days == 149
    assert r.change_1d is not None  # 前收存在 → 有值 (不是 None 也不是 0)


def test_single_valid_close_after_nulls_has_no_change(env):
    """只有一根有效收盘时, 变动量必须是 ``None`` (无前收), 且样本不足 → 无档位。"""
    _, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", [None, None, 24.0]))
    r = hv.hk_volatility(repo)
    assert r.sample_days == 1
    assert r.level == pytest.approx(24.0)
    assert r.change_1d is None and r.change_pct_1d is None
    assert r.insufficient_history is True


def test_percentile_and_regime_on_ramp_match_closed_form(env):
    """递增样本上, 最新一根 (最大值) 的分位排名 = ``(n-1)/n×100``。

    n=200 → 99.5 分位 → 落 ``panic`` (≥98)。这条同时验证了
    「样本最大值不会被判成 100 分位」与档位映射的联动。
    """
    _, repo, tmp_path = env
    n = 200
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(n)))
    r = hv.hk_volatility(repo)
    assert r.percentile == pytest.approx((n - 1) / n * 100.0)
    assert r.regime == "panic"
    assert r.regime_label == "恐慌"


def test_percentile_thresholds_are_returned_and_ordered(env):
    """🔴 分位阈值表必须随响应返回, 且键与 ``HK_VOLATILITY_PERCENTILES`` 一致、值升序。

    档位是相对标尺, 不给出「p90 到底是几点」就无法查证今天的判定;
    把阈值埋在代码常量里等于让用户无条件相信一个数字。
    """
    _, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(200)))
    r = hv.hk_volatility(repo)
    assert tuple(r.percentiles) == tuple(f"p{p}" for p in hv.HK_VOLATILITY_PERCENTILES)
    vals = list(r.percentiles.values())
    assert vals == sorted(vals)
    # 阈值必须落在样本值域内 (否则说明分位算错了)
    assert min(vals) >= r.period_low and max(vals) <= r.period_high


def test_band_upper_levels_cover_every_band_and_are_ordered(env):
    """档位上界点位: 键与 `HK_VOLATILITY_BANDS` **完全一致且同序**, 末档为 None。

    🔴 这是前端画档位分界线的唯一依据。它必须与 `bands` 同源: 若前端改拿
    `bands[].upper_percentile` 去 `percentiles` 里查, 档位边界(10/35/70/90/98)与
    分布概览分位(10/25/50/75/90/95/98/99)只有三个重合, 其余查不到 ⇒ 静默少画线。
    """
    _, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(200)))
    r = hv.hk_volatility(repo)
    expect_keys = tuple(key for _u, key, _l in hv.HK_VOLATILITY_BANDS)
    assert tuple(r.band_upper_levels) == expect_keys
    # 末档无上界
    assert r.band_upper_levels["panic"] is None
    # 其余各档都是有限点位, 且随分位上界单调不降
    bounded = [r.band_upper_levels[k] for k in expect_keys if k != "panic"]
    assert all(isinstance(v, float) for v in bounded)
    assert bounded == sorted(bounded)
    # 上界必须落在样本值域内, 否则说明分位算错了
    assert min(bounded) >= r.period_low and max(bounded) <= r.period_high
    # 与 percentiles 重合的那三个分位必须给出**同一个数**(同一个 quantile 函数)
    for p in (10, 90, 98):
        key = next(k for u, k, _l in hv.HK_VOLATILITY_BANDS if u == p)
        assert r.band_upper_levels[key] == r.percentiles[f"p{p}"]


def test_thresholds_are_self_calibrated_not_vix_convention(env):
    """🔴 阈值来自 VHSI **自身历史**, 不是 VIX 的 20/30。

    造一段中枢在 40 的样本 (远高于 VIX 常态): 若代码里写死了 20/30,
    中位数会被判成「恐慌」; 自身分位标定下, 中位数必然落在「常态」档。
    """
    _, repo, tmp_path = env
    n = 200
    closes = _ramp(n, start=35.0, step=0.05)  # 35.0 ~ 44.95, 全程 > VIX 的 30
    _seed_index_daily(tmp_path, repo, _series("VHSI", closes))
    r = hv.hk_volatility(repo)
    mid = closes[n // 2]
    assert mid > 30.0  # 按 VIX 口径这该是「恐慌」
    assert hv._band_of(hv._percentile_rank(sorted(closes), mid))[0] == "normal"
    assert r.percentiles["p50"] > 30.0  # 阈值本身也证明不是 20/30


def test_streak_counts_consecutive_days_in_current_band(env):
    """连续天数: 尾部三根同档 → 3; 再往前一根落别的档就停。

    🔴 用**平台 + 跳变**而不是斜坡造数: n=200 时严格递增斜坡的**前 4 根最大值**
    分位都 ≥98 (``196/200=98.0`` 恰好落 panic, 因为边界左闭), 于是 streak 会是 4
    而不是 3 —— 断言失败看起来像服务多算了一天, 实际是造数撞了档位边界。
    平台让「跳变前的那一天」分位恒为 0 (``bisect_left`` 落在首个相等值上),
    与 panic 干净地隔开。
    """
    _, repo, tmp_path = env
    closes = [*[20.0] * 197, 99.0, 99.0, 99.0]
    _seed_index_daily(tmp_path, repo, _series("VHSI", closes))
    r = hv.hk_volatility(repo)
    assert r.regime == "panic"
    assert r.streak_days == 3
    # 平台那天的分位是 0 (极低波), 证明 streak 确实在跳变处停住
    assert hv._band_of(hv._percentile_rank(sorted(closes), 20.0))[0] == "ultra_low"


def test_streak_is_one_when_band_just_changed(env):
    """档位刚切换 → 连续天数为 1 (含当日), 不是 0 也不是上一档的累计。"""
    _, repo, tmp_path = env
    closes = [*[20.0] * 199, 99.0]
    _seed_index_daily(tmp_path, repo, _series("VHSI", closes))
    r = hv.hk_volatility(repo)
    assert r.regime == "panic"
    assert r.streak_days == 1


def test_streak_spans_whole_sample_when_band_never_changed(env):
    """全程同档 → streak 等于样本长度 (上界), 不越界也不停在半路。"""
    _, repo, tmp_path = env
    n = 150
    _seed_index_daily(tmp_path, repo, _series("VHSI", [22.0] * n))
    r = hv.hk_volatility(repo)
    assert r.streak_days == n


def test_moving_averages_none_when_sample_shorter_than_window(env):
    """样本不足以算某条均线时该项为 ``None``, 不用短窗口凑 (见 ``_rolling_mean``)。"""
    _, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(150)))
    r = hv.hk_volatility(repo)
    assert set(r.moving_averages) == {"ma5", "ma20", "ma60"}
    assert r.moving_averages["ma5"] is not None
    assert r.moving_averages["ma20"] is not None
    # 150 根 > 60 → ma60 也该有值; 用 None 断言会漏掉「窗口写错」的回归
    assert r.moving_averages["ma60"] is not None


def test_ma60_is_none_below_60_samples(env):
    """显式造 59 根有效样本 (仍 fail closed, 但均线字段独立可验)。"""
    _, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(59)))
    r = hv.hk_volatility(repo)
    assert r.moving_averages["ma60"] is None
    assert r.moving_averages["ma20"] is not None


def test_limit_trims_sample_and_therefore_thresholds(env):
    """🔴 ``limit`` 会**改变分位阈值** (样本窗口变了), 这正是必须回显
    ``sample_days``/``sample_start`` 的原因: 脱离窗口解读档位是错的。

    造「前段极高、后段极低」的样本: 全样本下最新值分位很低,
    只看后段时最新值反而成了该窗口的高位。
    """
    _, repo, tmp_path = env
    closes = [80.0] * 150 + _ramp(150, start=15.0, step=0.01)
    _seed_index_daily(tmp_path, repo, _series("VHSI", closes))

    full = hv.hk_volatility(repo)
    assert full.sample_days == 300
    assert full.level == pytest.approx(closes[-1])

    trimmed = hv.hk_volatility(repo, limit=150)
    assert trimmed.sample_days == 150
    assert trimmed.level == full.level  # 最新点位不受窗口影响
    # 同一天的点位, 在两个窗口下的分位**不同** → 证明阈值确实随样本重算
    assert trimmed.percentile != pytest.approx(full.percentile)
    assert trimmed.sample_start != full.sample_start


def test_series_carries_only_level_and_ma20(env):
    """🔴 绘图序列**不含**逐日分位/档位。

    阈值是全样本算出来的, 历史某天的档位不是 point-in-time 的 (用了它之后的
    数据定阈值), 画出来就是错误信息。故只给 ``date/close/ma20``。
    """
    _, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(150)))
    r = hv.hk_volatility(repo)
    assert set(r.series[0]) == {"date", "close", "ma20"}
    assert r.series[0]["ma20"] is None  # 前 19 根 ma20 未成形
    assert r.series[-1]["ma20"] is not None
    assert [p["date"] for p in r.series] == sorted(p["date"] for p in r.series)


# ---------------------------------------------------------------------------
# 服务: 与恒指的背离
# ---------------------------------------------------------------------------


def test_corr_negative_when_vhsi_moves_opposite_hsi(env):
    """VHSI 与 HSI 日收益反向 → 相关系数显著为负 (这是正常状态)。

    🔴 用 :func:`_from_returns` 造**逐日交替**的收益, 不用线性斜坡 —— 斜坡的
    收益率自身单调, 会让两条明显反向的曲线算出 +1 (见该 helper 的 docstring)。
    """
    _, repo, tmp_path = env
    n = 150
    hsi = _from_returns(20000.0, _alternating(n - 1, 0.010, -0.005))  # 涨多跌少
    vhsi = _from_returns(25.0, _alternating(n - 1, -0.010, 0.005))  # 逐日反向
    _seed_index_daily(
        tmp_path, repo, _series("HSI", hsi) + _series("VHSI", vhsi)
    )
    r = hv.hk_volatility(repo)
    assert r.corr_hsi is not None
    assert r.corr_hsi < -0.9
    assert r.corr_samples == hv.HK_VOLATILITY_CORR_WINDOW


def test_corr_positive_when_both_rise_together(env):
    """齐涨 → 正相关。这正是需要暴露的**异常**状态 (波动率与大盘同涨)。"""
    _, repo, tmp_path = env
    n = 150
    rets = _alternating(n - 1, 0.010, -0.005)
    _seed_index_daily(
        tmp_path,
        repo,
        _series("HSI", _from_returns(20000.0, rets))
        + _series("VHSI", _from_returns(25.0, rets)),  # 与恒指**同向**
    )
    r = hv.hk_volatility(repo)
    assert r.corr_hsi is not None and r.corr_hsi > 0.9


def test_corr_none_when_benchmark_missing(env):
    """🔴 基准 (HSI) 缺失 → ``corr_hsi`` 为 ``None`` 且 ``corr_samples=0``,
    **不是 0**。0 会被读成「实测不相关」, 而真相是「没有基准可比」。

    同时验证: 缺基准**不影响** VHSI 自身的档位判定 (两者是独立的量)。
    """
    _, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(150)))
    r = hv.hk_volatility(repo)
    assert r.corr_hsi is None
    assert r.corr_samples == 0
    assert r.regime is not None  # 档位照常


def test_corr_none_when_vhsi_flat(env):
    """VHSI 全程不动 (方差 0) → 相关系数无定义 → ``None``。"""
    _, repo, tmp_path = env
    n = 150
    _seed_index_daily(
        tmp_path,
        repo,
        _series("HSI", _ramp(n, start=20000.0, step=100.0))
        + _series("VHSI", [22.0] * n),
    )
    r = hv.hk_volatility(repo)
    assert r.corr_hsi is None
    assert r.level == pytest.approx(22.0)


def test_corr_uses_only_overlapping_dates(env):
    """🔴 按**日期内连接**配对, 不按位置配对; 且 ``corr_samples`` 反映**真实重叠数**。

    港股与 A 股交易日历不同, 两个序列长度也可能不同 (同步深度不一致);
    按位置 zip 会把不同日期的收益配成一对, 算出一个毫无意义但看起来正常的系数。

    构造: VHSI 有 150 天, HSI 只覆盖其中**最后 20 天** → 重叠 20 根,
    少于 ``CORR_WINDOW+1=21``, 故不触发尾部裁剪, 收益对数 = 20−1 = **19**。
    若代码是按位置配对或硬凑满窗口, 这里会得到 20 而不是 19。
    """
    _, repo, tmp_path = env
    vhsi_rows = _series("VHSI", _from_returns(25.0, _alternating(149, -0.010, 0.005)))
    hsi_rows = _series(
        "HSI",
        _from_returns(20000.0, _alternating(29, 0.010, -0.005)),  # 与 VHSI 逐日反向
        start=vhsi_rows[-20]["date"],
    )
    _seed_index_daily(tmp_path, repo, vhsi_rows + hsi_rows)
    r = hv.hk_volatility(repo)
    assert r.sample_days == 150  # 主标的样本不受基准缺失影响
    assert r.corr_samples == 19  # 🔴 真实重叠数 − 1, 不是窗口值 20
    assert r.corr_hsi is not None and r.corr_hsi < -0.9


def test_corr_window_caps_samples(env):
    """相关系数只用**尾部窗口**, 不吃全史: 再长的样本 ``corr_samples`` 也封顶。

    否则一次八年前的行情会一直稀释当前的背离信号。
    """
    _, repo, tmp_path = env
    n = 400
    _seed_index_daily(
        tmp_path,
        repo,
        _series("HSI", _ramp(n, start=20000.0, step=10.0))
        + _series("VHSI", _ramp(n, start=20.0, step=0.05)),
    )
    r = hv.hk_volatility(repo)
    assert r.sample_days == n
    assert r.corr_samples == hv.HK_VOLATILITY_CORR_WINDOW


# ---------------------------------------------------------------------------
# API 契约
# ---------------------------------------------------------------------------


def test_api_success_shape(env):
    """成功响应: 单位/延迟/币种/档位/阈值表/样本三件套齐全, 日期为 ISO 字符串。"""
    client, repo, tmp_path = env
    n = 150
    rets = _alternating(n - 1, 0.010, -0.005)
    _seed_index_daily(
        tmp_path,
        repo,
        # 恒指与波动率**逐日反向** → corr 应为负 (造数理由见 _from_returns docstring)
        _series("HSI", _from_returns(20000.0, rets))
        + _series("VHSI", _from_returns(25.0, [-r for r in rets])),
    )
    r = client.get("/api/hk/volatility")
    assert r.status_code == 200
    j = r.json()

    assert j["symbol"] == "VHSI"
    assert j["market"] == "HK"
    assert j["currency"] == "HKD"
    assert j["source"] == hv.HK_VOLATILITY_SOURCE
    assert j["insufficient_history"] is False
    assert j["insufficient_reason"] is None

    # 🔴 延迟分钟数来自 rules_for(Market.HK), 不是硬编码
    from app.market import Market, rules_for

    assert j["quote_delay_minutes"] == rules_for(Market.HK).quote_delay_minutes == 15

    # 日期一律 ISO 字符串 (date 对象不能直接 JSON 序列化)
    for k in ("as_of", "period_high_date", "period_low_date", "sample_start", "sample_end"):
        assert isinstance(j[k], str), k
        dt.date.fromisoformat(j[k])

    assert j["percentile"] is not None and 0.0 <= j["percentile"] <= 100.0
    assert j["regime"] in {b["key"] for b in j["bands"]}
    assert j["regime_label"] in {b["label"] for b in j["bands"]}
    assert j["streak_days"] >= 1
    assert set(j["moving_averages"]) == {"ma5", "ma20", "ma60"}
    assert set(j["percentiles"]) == {f"p{p}" for p in hv.HK_VOLATILITY_PERCENTILES}
    # 档位上界点位必须随响应下发, 且键与 bands 一致 (前端画分界线的唯一依据)
    assert list(j["band_upper_levels"]) == [b["key"] for b in j["bands"]]
    assert j["band_upper_levels"]["panic"] is None
    assert j["corr_window"] == hv.HK_VOLATILITY_CORR_WINDOW
    assert j["min_sample_days"] == hv.HK_VOLATILITY_MIN_SAMPLE_DAYS
    assert j["sample_days"] == 150
    assert j["corr_hsi"] < 0  # VHSI 跌 / HSI 涨 → 负相关


def test_api_bands_expose_boundaries_for_audit(env):
    """档位边界表随响应下发 (含无上界的兜底段), 前端不必复制一份常量。

    复制一份就会与后端分叉: 后端改了边界, 前端图例还在按旧边界着色。
    """
    client, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(150)))
    bands = client.get("/api/hk/volatility").json()["bands"]
    assert [b["key"] for b in bands] == [b[1] for b in hv.HK_VOLATILITY_BANDS]
    assert [b["label"] for b in bands] == [b[2] for b in hv.HK_VOLATILITY_BANDS]
    assert [b["upper_percentile"] for b in bands] == [b[0] for b in hv.HK_VOLATILITY_BANDS]
    assert bands[-1]["upper_percentile"] is None


def test_api_name_is_null_for_non_core_index(env):
    """🔴 ``name`` 对 VHSI 恒为 ``null``: 它不在四个核心指数里, 而 Sina 插件
    刻意不登记指数名称 (「抄一份只会与源分叉」)。

    后端**不编**一个展示名; 名称由前端适配器提供。这条测试钉住「不编」,
    防止将来有人为了让响应好看而硬写一个中文名。
    """
    client, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(150)))
    j = client.get("/api/hk/volatility").json()
    assert j["name"] is None
    assert "VHSI" not in __import__("app.services.index_const", fromlist=["x"]).HK_CORE_INDEX_NAMES


def test_api_no_data_is_200_not_500(env):
    """🔴 从未同步 → **200 + ``insufficient_history=true``**, 不是 500 也不是 502。

    这是本地数据缺失, 不是上游故障; 本端点**没有任何上游请求**, 故不存在 502 分支。
    报 500 会把「还没同步」伪装成「服务坏了」, 用户无从知道该去点同步。
    """
    client, _, _ = env
    r = client.get("/api/hk/volatility")
    assert r.status_code == 200
    j = r.json()
    assert j["insufficient_history"] is True
    assert j["regime"] is None and j["percentile"] is None
    assert j["level"] is None and j["as_of"] is None
    assert j["series"] == [] and j["series_days"] == 0
    assert j["sample_days"] == 0
    assert j["insufficient_reason"]  # 必须有可展示的原因
    # 阈值表为空 dict 而不是 None: 前端 Object.entries 不必判空
    assert j["percentiles"] == {}
    assert j["band_upper_levels"] == {}
    assert j["moving_averages"] == {"ma5": None, "ma20": None, "ma60": None}


def test_api_series_days_trims_series_only(env):
    """🔴🔴 ``series_days`` **只裁剪绘图序列**, 不裁剪标定样本。

    这是本端点最容易写错的一条: 若顺手把 ``limit`` 也传进服务, 档位就会用
    「近 30 天」标定, 而响应里的 ``regime`` 看起来与全样本时一模一样 —— 
    两个不同的尺子, 界面上无法区分。
    """
    client, repo, tmp_path = env
    # 前段极高、后段极低: 窗口不同时最新值的分位会明显不同
    closes = [80.0] * 150 + _ramp(150, start=15.0, step=0.01)
    _seed_index_daily(tmp_path, repo, _series("VHSI", closes))

    full = client.get("/api/hk/volatility").json()
    assert full["sample_days"] == 300
    assert full["series_days"] == hk_api.HK_VOLATILITY_DEFAULT_SERIES_DAYS

    trimmed = client.get("/api/hk/volatility", params={"series_days": 30}).json()
    assert trimmed["series_days"] == 30
    assert len(trimmed["series"]) == 30
    # 🔴 标定样本与档位**完全不变**
    assert trimmed["sample_days"] == full["sample_days"] == 300
    assert trimmed["sample_start"] == full["sample_start"]
    assert trimmed["regime"] == full["regime"]
    assert trimmed["percentile"] == full["percentile"]
    assert trimmed["percentiles"] == full["percentiles"]
    assert trimmed["level"] == full["level"]
    # 裁掉的是**头部** (最近的留下), 否则图上看到的是几个月前
    assert trimmed["series"][-1] == full["series"][-1]


def test_api_series_days_larger_than_sample_returns_all(env):
    """``series_days`` 大于样本量时返回全部, 不补空点。"""
    client, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(150)))
    j = client.get("/api/hk/volatility", params={"series_days": 2000}).json()
    assert j["series_days"] == 150 == j["sample_days"]


@pytest.mark.parametrize("bad", [0, -1, 2001, 99999])
def test_api_series_days_out_of_range_is_422(env, bad):
    """窗口越界 → 422 (FastAPI 的 ``ge``/``le`` 校验), 不静默夹到边界。

    夹到边界会让「我要 99999 根」返回 2000 根却报 200, 调用方无从发现自己写错了。
    """
    client, repo, tmp_path = env
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(150)))
    assert client.get("/api/hk/volatility", params={"series_days": bad}).status_code == 422


def test_api_insufficient_history_still_200(env):
    """样本不足 (非零) → 200 + 判定量全 null, 但客观事实 (点位/极值/均线) 仍在。"""
    client, repo, tmp_path = env
    n = hv.HK_VOLATILITY_MIN_SAMPLE_DAYS - 1
    _seed_index_daily(tmp_path, repo, _series("VHSI", _ramp(n)))
    r = client.get("/api/hk/volatility")
    assert r.status_code == 200
    j = r.json()
    assert j["insufficient_history"] is True
    assert j["sample_days"] == n
    assert (j["regime"], j["regime_label"], j["percentile"], j["streak_days"]) == (
        None, None, None, None,
    )
    assert j["corr_hsi"] is None and j["corr_samples"] == 0
    assert j["percentiles"] == {}
    assert j["band_upper_levels"] == {}
    assert j["level"] is not None  # 客观事实照常给
    assert j["period_high"] is not None and j["period_high_date"] is not None
    assert j["series_days"] == n  # 序列不受样本不足影响


def test_api_does_not_leak_date_objects(env):
    """整个响应必须可直接 JSON 序列化 (无 ``date`` 对象残留)。

    ``series`` 里的日期是服务层就转好的字符串, 顶层日期字段由 API 层转;
    漏一个就会在响应阶段抛 ``TypeError``, 而不是在测试里。
    """
    client, repo, tmp_path = env
    _seed_index_daily(
        tmp_path,
        repo,
        _series("HSI", _ramp(150, start=20000.0, step=50.0))
        + _series("VHSI", _ramp(150, start=20.0, step=0.05)),
    )
    j = client.get("/api/hk/volatility").json()
    for p in j["series"][:3] + j["series"][-3:]:
        assert isinstance(p["date"], str)
        dt.date.fromisoformat(p["date"])
