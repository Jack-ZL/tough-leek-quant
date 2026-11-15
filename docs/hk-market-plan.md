# 港股支持设计方案

> 状态: ⬜ 方案定稿, 未开始实施（2026-11）。本文档是 P2-28 的实施依据。
> 遵循 [CONTRIBUTING.md](../CONTRIBUTING.md) 的数据口径（§3）、数据源插件化（§4）、能力路由矩阵契约与测试矩阵要求。
> 涉及扩展点时同时遵循 [secondary-development.md](secondary-development.md)、[plugin-development.md](plugin-development.md)、[custom-data-source.md](custom-data-source.md)。

## 1. 目标与非目标

**目标**

- 让 TSP 能看港股行情、做港股 AI 个股分析（含多空辩论）、看港股财务概况、看港股板块与指数，**全部复用 A 股现有页面与入口**，不 fork 页面。
- 建立可复用的 **market 维度地基**，为将来接美股铺路（本期不实现美股）。
- **对 A 股零影响**：A 股行为、数据、性能、启动路径均不变，既有测试全绿即视为达标。

**非目标**

- 不做美股（时区/夏令时/lot size/财报口径差异更大，等地基验证过再议）。
- 不做港股回测、因子挖掘、策略生成、模拟盘、实盘下单。
- 不做港股概念板块、五档盘口（**无免费数据源**，见 §3.4）。
- 不做港股连板/涨停/情绪周期/龙虎榜（A 股独有语义，港股无对应物）。
- 不做跨市场汇率换算与多货币合并账务（货币仅展示层标注，存储始终本币）。
- 不做 CCASS 聪明钱（成本高，列为后续再议，见 §16）。

## 2. 市场规则模型

新建 `backend/app/market.py`，作为**全项目唯一的市场规则真源**。

```python
class Market(str, Enum):
    CN = "CN"
    HK = "HK"

@dataclass(frozen=True)
class MarketTradingRules:
    market: Market
    currency: str                 # CN: "CNY"  HK: "HKD"
    timezone: str                 # 两者均 UTC+8, 但显式声明: "Asia/Shanghai" / "Asia/Hong_Kong"
    sessions: ...                 # 必须支持「按日期变长」以表达半日市, 见 §5
    t_plus_1: bool                # CN: True   HK: False (T+0, T+2 交收不影响交易)
    lot_size: int | None          # CN: 100    HK: None → 按标的元数据 instruments_hk.lot_size
    has_price_limit: bool         # CN: True   HK: False
    tick_size: ...                # CN: 固定 0.01   HK: 按股价分档 spread table
    commission_rate: float        # HK: 0.0003
    commission_min: float         # HK: 3.0 (HKD)
    stamp_duty_rate: float        # HK: 0.001, 卖出单边
    transfer_fee_rate: float      # HK: 0.0 (无过户费)
    default_benchmark: str        # CN: "000001.SH"  HK: "HSI"
    quote_delay_minutes: int      # CN: 0   HK: 15
    financial_report_currency_fixed: bool  # CN: True  HK: False ← 见 §5 报告货币
```

配套函数：

- `rules_for(market) -> MarketTradingRules`
- `infer_market(symbol) -> Market`：**推断场景**才兜底 CN
- `is_valid_symbol(symbol, market) -> bool`
- symbol 判据**一套两种载体**：Python 正则 + SQL 正则同源生成（`market_symbol_sql_regex()` 锚定 `^({pattern})$`），避免两处判据漂移

**未知市场处理（重要）**：**过滤场景返回 `None`，不兜底 CN**。QuantMind `market_rules.py:395-413` 注释明写：「否则『不传 market』会静默变成『只看 A 股』」。只有 `infer_market` 这类推断场景才兜底 CN。

**symbol 口径**：**5 位 `00700.HK`**（akshare / 东财原生格式），**8 开头创业板保留 5 位**。

> ⚠️ 全项目**单一口径**。QuantMind 的债务是 `ccass_top50` 用 5 位 stock_code、`hsgt_south` 用 4 位+.HK，两种口径并存。TSP 不重犯。
> 注：QuantMind 自身用 4 位 `0700.HK`，那是它的约定，不跟随。

**现有三个模块改为 market 分派**（`cn` 为默认值 → **52 个消费方零改动**）：

| 模块 | 现状 | 改造 |
|---|---|---|
| `backend/app/market_time.py` | `CN_TZ`、`_MORNING_START=9:30`/`_MORNING_END=11:30`/`_AFTERNOON_START=13:00`/`_AFTERNOON_END=15:00`、`_TRADING_TOTAL_MINUTES=240` 全是模块级常量 | 时段/时区查规则表；`cn_now()`/`cn_today()`/`in_continuous_session()`/`trading_minutes_elapsed_from_dt()` 加 `market="cn"` 默认参数 |
| `backend/app/services/trading_day.py` | A 股专属 oracle：fuyao 日历 → tickflow basket `_BASKET=("000001.SZ","600519.SH","600036.SH","601318.SH","000651.SZ")` max(timestamp)==today → None=unknown | per-market oracle；港股篮子 `("00700.HK","00005.HK","01398.HK","00388.HK","09988.HK")`；判定结果**带 provenance**（`(bool, source)`） |
| `backend/app/price_limits.py` | `price_limit_pct(symbol, trade_date, *, is_risk_warning)` :40-55、`polars_price_limit_pct` :140-162 靠 symbol 前缀判（300/301/302 创业、688/689 科创、`.BJ` 北交） | `has_price_limit=False` → 返回 `None`；enriched 的 `limit_up_price`/`limit_down_price` 恒 null |

**护栏**：港股代码进 A 股处理层**当场报错**（学 astock `#43`），不静默降级。

## 3. 数据源

### 3.1 选型：直连为主，扩现有插件；akshare 仅作财务类兜底的可选 extra

> **2026-11-15 决策变更。** 原方案是「新增 `plugins/akshare_hk/` 独立插件、不扩现有插件」，
> 理由是「扩现有插件会修改 A 股在用的 `symbol_to_secid`，有回归风险」。**该理由经核实站不住**：
> `eastmoney/provider.py:81` 的 `symbol_to_secid` 是 18 行纯函数，加港股只是在末尾
> `return None` 之前插一个 `if suffix == "HK": return f"116.{code}"` 分支，**纯增量、
> 碰不到 SH/SZ/BJ 任何既有分支**，且 PR1 已落地 `assert_symbol_market` 护栏与全量回归。
> 用户已选定：**行情/K线/指数直连；财务类先试直连，实在不行再把 akshare 作为可选 extra。**

**为什么直连优先（实测依据）**：

| 事实 | 证据 |
|---|---|
| 港股行情/K线**不需要 akshare，也不需要 JS 引擎** | 东财 `clist?fs=m:128 t:3` 与腾讯 `hkfqkline/get?param=hk00700,day,,,5,qfq` 均为**纯 HTTP** 实测通过（2026-11-15） |
| 现有 6 个插件**全部直连、零 akshare 依赖** | `grep -rl akshare app/plugins/` → 无命中。引入 akshare 会让港股插件成为**唯一架构异类** |
| 直连能复用**已踩坑踩出来的限速/熔断** | `eastmoney/client.py:5-14` 记着实测教训「短时间 ~6 个请求即触发 push2 的 IP 级临时封禁」，三主机各一套独立限速+熔断+封禁池。akshare 无此机制，且其 `#7465`(2026-10-30) 改为「东财 push2 限流时**不再重试**」→ 走 akshare 反而更易被封 |
| akshare 返回 pandas，本项目全栈 polars | 每个接口都要多一层转换 |

**akshare 保留为可选 extra 的唯一理由**：财务三表 / 36 列指标 / 分红 / 人气榜这类接口**解析繁琐**，akshare 已封装 40+ 个。策略是**先试直连东财 datacenter**，确实啃不动再启用 extra（不装则卡片置灰带安装按钮，**绝不影响 A 股启动**）。QuantMind 的生产实测（`compare_akshare_yahoo.py` 抽样对比后「以 akshare 为准」、`akshare_hk_fundamental.py:5` 注「替换雅虎空/弱数据」）说明**财务类数据 akshare 质量确实更好**，这是保留它作兜底的依据，不是全量采用它的依据。

### 3.1.1 直连端点实测清单（2026-11-15 活体验证）

| 用途 | 端点 | 实测结果 |
|---|---|---|
| 港股全市场股票列表 | `push2.eastmoney.com/api/qt/clist/get?fs=m:128 t:3,m:128 t:4,m:128 t:1,m:128 t:2` | **total=4748** |
| 港股主板列表 | 同上但 `fs=m:128 t:3` | **total=2621** |
| 港股日K（东财） | `push2his.eastmoney.com/api/qt/stock/kline/get?secid=116.00700&klt=101&fqt=1` | 腾讯控股，末根 `2026-11-13,393.000,386.600,395.200,385.600,31493353` |
| 港股日K（腾讯，**含前复权**） | `web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?param=hk00700,day,,,5,qfq` | code=0，5 根，末根同上。⚠️ 该 qfq 是**等差**投影，**不能当复权因子源**，见 §3.6 |

⚠️ **三个必须写进 PR2 的坑**：

1. **市场码是 `m:128` 不是 `m:116`。** `m:116` 返回 **17580** 条，混入大量窝轮/牛熊证（实测样本：`14192 中寿信证七一购A`、`69128 华虹汇丰八甲熊J`）。`m:128` 才是股票。子类实测：`t:1`=493(REITs) `t:2`=1324(债券) `t:3`=2621(主板) `t:4`=310(GEM) `t:5`=5903(窝轮牛熊证)。
   **分隔符 `+` 与空格等价**（2026-11-15 复测：`m:128+t:3` 与 `m:128 t:3` 均返回 total=2621）——URL 编码后同为空格，与既有 `FS_ALL_A` 的 `+` 写法一致即可。
2. **单标的 secid 前缀 `116.` 与 `128.` 都能用**，实测返回完全相同的数据。**已定：单标的统一用 `116.`，列表过滤统一用 `m:128`**（两者是不同命名空间：secid 是标的命名空间，`fs` 是过滤命名空间）。写测试钉住，否则两套前缀混用会在缓存键与去重上出问题。
3. **港股有双柜台（港币/人民币），同一公司两个代码。** 规律比最初以为的更宽：**人民币柜台代码 = 港币柜台代码首位 `0`→`8`**，名称多一个 `-R` 后缀。实测主板 24 个：`89988 阿里巴巴-WR↔09988 阿里巴巴-W`、`89888 百度集团-WR↔09888`、`89618 京东集团-SWR↔09618`、`86618 京东健康-R↔06618`、`83690 美团-WR↔03690`、`82388 中银香港-R↔02388`、`82333 长城汽车-R↔02333`、`82331 李宁-R↔02331` 等。
   ⚠️ **不能只按 `89` 开头识别**——`82xxx`/`83xxx`/`86xxx` 同样是人民币柜台，只查 `89` 会漏掉 21 个。

   **已定策略（2026-11-15）**：两个代码都入库，`instruments_hk` 加 `is_cny_counter: bool` 标记；**所有市场级统计（成交额合计、涨跌家数、量比分母、行业排名）只计港币柜台**。

   为什么保留而不是直接不入库：人民币柜台是真实可交易标的，用户搜 `89988` 时应能命中并看到行情，入库时过滤会让搜索与个股页 404。为什么用标记而不是入库时合并：把「是否参与统计」的决定权留在查询层，日后想改口径（例如某些统计需含双柜台）不必重跑全量入库。**该策略影响 PR6 行业排名正确性，PR2 落地时必须有测试钉住。**
4. 🔴 **成交量单位与 A 股不同：港股是「股」，A 股是「手」。** 这是本次最容易静默算错的一处，详见 §3.1.2。

### 3.1.2 🔴 成交量单位：港股「股」vs A股「手」（PR2 最高危的一处）

**实测结论（2026-11-15，两条独立交叉验证）**：东财港股接口的成交量单位是**股**，而 A 股是**手**（1 手 = 100 股）。

| 验证路径 | 数据 | 结论 |
| --- | --- | --- |
| clist 快照自洽 | `89988` f5=86000、f6=6679940 元、f2=76.8 → 86000×76.8≈6.6M≈f6 | f5 是**股**（若是手则成交额应为 6.6 亿，差 100 倍） |
| kline × 换手率 | `00700` 2026-11-13 volume=31493353，腾讯同行返回换手率 `0.350`%；腾讯总股本≈91.9 亿股 → 31.5M/9.19B=0.34% | volume 是**股**（与换手率吻合） |

**为什么危险**：现有代码把「volume 单位 = 手」**硬编码在 6 处 / 5 个文件**，全部以 `×100` 的形式出现：

| 位置 | 公式 | 港股直接透传的后果 | PR2a 状态 |
| --- | --- | --- | --- |
| `indicators/pipeline.py:813`（`compute_limit_signals`） | `turnover_rate = volume × 10000 / float_shares` | **换手率高 100 倍** | ✅ 已改走 `shares_per_volume_unit` |
| `indicators/pipeline.py:2422`（`_compute_limit_signals_today`） | 同上（盘中增量路径） | 同上 | ✅ 已改 |
| `strategy/scoring.py:179` | `vwap = amount / (volume × 100)` | **VWAP 低 100 倍** | ⏸ 延后（见下） |
| `backtest/engine.py:1979` | `vwap = amount / (volume × 100)` | 同上 | ⏸ 延后 |
| `strategy/intraday_features.py:106` | `vwap = cum_amt / (cum_vol × 100)` | 同上（分钟线） | ⏸ 延后 |
| `api/monitor_rules.py:558,675`、`strategy/monitor.py:1766,1796` | `封单额 = vol × 100 × close` | 港股无涨跌停封单概念 | ⏸ 延后（不可达） |

**⏸ 延后的 4 处为什么可以延后**（不是遗漏，是范围判断）：

- `scoring.py` / `backtest/engine.py` 的 VWAP：港股在 PR2a **不进入打分与回测链路**（选股器与回测仍只读 A 股 unified 视图，闸3 已用 `test_unified_views_exclude_hk` 钉死）。等港股接入打分/回测的那个 PR 必须一并改这两处，否则 VWAP 低 100 倍会让港股永远打不出分。
- `intraday_features.py`：分钟线，港股分钟数据 PR2a 不落库（`kline_hk_minute` 目录尚未启用）。
- `monitor_rules.py` / `monitor.py` 的封单额：港股**无涨跌停制度**（`HK_RULES.has_price_limit=False`），封单监控对港股逻辑上不可达。

这 4 处**必须在对应功能接入港股的同一个 PR 里改完**，不能靠「以后再说」——建议在接港股打分/回测时把本表当作 checklist。

注意 **量比（`vol_ratio_5d`）不受影响** —— 它是「今日量 / 前 5 日均量」的**比值**，分子分母同单位即约掉。半日市影响的是**时间**折算（330/150），与成交量单位是两件独立的事，不要混为一谈。

**已定方案**：不在 provider 层把港股 volume 折算成「手」。理由：港股每手股数（board lot）**逐股不同**（腾讯 100、汇丰 400、中国银行 1000），折算需要每只标的的 `lot_size`，且碎股（odd lot）会产生非整数手 —— 把一个无损的原始量纲换成有损且依赖外部元数据的量纲，是错的方向。

改为：**内部 volume 单位随市场而变，换算系数集中到 `market.py` 一处**：

```python
def shares_per_volume_unit(market) -> float:
    """内部 volume 单位 → 股。CN: volume 单位=手 → 100.0; HK: volume 单位=股 → 1.0。"""
```

上表 6 处的 `× 100` / `× 10000` 改为经此函数取值，**market 参数仅关键字且默认 `CN`**。这样：

- A 股路径算术**完全不变**（系数仍是 100），存量测试必须一行不改即全绿 —— 这是本改动的验收标准；
- 港股走 `market=Market.HK` 时系数为 1，换手率与 VWAP 正确；
- 单位知识只有一处，将来接美股（单位也是股）零成本。

⚠️ **PR2 落地顺序要求**：`shares_per_volume_unit` 必须**先于**任何港股 K 线落库上线。否则港股数据一旦进 enriched，错误的换手率会被物化进 parquet，事后修正需要重算全量历史。



### 3.2 四源清单

> **读法（2026-11-15 决策后）**：下表的 `stock_hk_*_em` 等是 **akshare 的函数名，仅作
> 「上游有哪些接口、字段长什么样」的规格索引**，不代表我们要调 akshare。行情/K线/指数类
> 按 §3.1.1 直连；财务类先试直连东财 datacenter，啃不动的才落到 akshare extra。
> 每个接口在 PR2 落地时都要**活体验证一次**并记录真实端点与字段，不得照抄函数名当结论。

| 源 | 后缀 | 覆盖 |
|---|---|---|
| 东方财富 | `_em` | 日K(`stock_hk_hist_em`, 可选复权) · 实时(`stock_hk_spot_em` 全市场 ~4528 只 / `stock_hk_main_board_spot_em` 主板 / `stock_hk_famous_spot_em` 知名) · 分钟(`stock_hk_hist_min_em`, 近 5 交易日) · **财务三表**(`stock_financial_hk_report_em`) · **财务指标 36 列**(`stock_financial_hk_analysis_indicator_em`) · 最新指标(`stock_hk_financial_indicator_em`) · 分红(`stock_hk_dividend_payout_em`) · 证券资料(`stock_hk_security_profile_em`) · 公司资料(`stock_hk_company_profile_em`) · 行业对比三件套(`stock_hk_growth/valuation/scale_comparison_em`) · 人气榜(`stock_hk_hot_rank_em`/`_latest_em`/`_detail_em`/`_detail_realtime_em`) · AH 比价(`stock_zh_ah_spot_em`) · 港股指数(`index_hk_spot_em`/`index_hk_daily_em`) |
| 新浪 | `_sina` | 日K(`stock_hk_daily_sina`, **含前后复权因子**) · 实时(`stock_hk_spot_sina`, 含中英文名+交易类型) · 指数(`index_hk_spot_sina`/`index_hk_daily_sina`) |
| 腾讯 | `_tx` | AH 股名单/实时/日线(`stock_zh_ah_name_tx`/`_spot_tx`/`_daily_tx`) |
| 雪球 | `_xq` | `stock_individual_basic_info_hk_xq` 公司基本信息（字段是缩写黑话 `comcnname`/`numtissh`/`ispr`/`nrfd`/`mbu`，需映射表，质量一般，**仅兜底**） |

### 3.3 多源兜底策略

**双源以上（可主备切换）**：日K(em+sina) · 实时(em+sina) · 指数(em+sina) · AH(em+tx) · 基本信息(em+xq)

**单源无兜底**：财务三表、财务指标、分钟K、每手股数、所属行业、**港股分红/复权因子** —— **全是东财**。

且 akshare `#7465`（2026-10-30）提交标题为「**统一重试逻辑，东方财富 push2 限流时不再重试**」→ 东财限流时 akshare **直接失败**。**这正是 §3.1 选直连的理由之一**：直连走 `eastmoney/client.py` 已有的每主机令牌桶+熔断+封禁池，限流时是我们自己退避重试，而不是直接失败。

因此单源能力的兜底只能是：

1. **本地 last-good 快照**：tmp+rename 原子写、同 key 60s 写盘节流、7 天 maxAge、损坏/超龄**如实报错**；降级返回旧值 **+ 新鲜度标记**（不是 502，也不是假装是新数据）
2. **补不上就显式说出来**：CONTRIBUTING §4「When a provider lacks a capability, the system must clearly indicate it, skip the feature, or fail closed. Silently switching to wrong data or wrong semantics is forbidden.」

### 3.4 确认的数据缺口

| 缺口 | 验证方式 |
|---|---|
| **港股概念板块** | 东财板块中心 `push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:{1,2,3}` 实测：`t:1` 地域 31 个、`t:2` 行业 496 个、`t:3` 概念 554 个，**全是 A 股**（`BK0882 猪肉概念` 等）；`t:4/t:5/t:6` 返回空。akshare `stock_board.html` 里港股只有**人气榜**，板块接口全是 `stock_board_concept_*_em`/`stock_board_industry_*_em`/`*_ths`（A 股）。**东财网页版也没有港股板块** |
| **港股五档盘口 depth5** | akshare 无港股 depth5 接口 |
| **恒生指数成份股名单** | akshare `index_stock_cons*` 只有 A 股/中证/国证/申万。成份权重可走**中证港股通系列**（`index_stock_cons_weight_csindex`，QuantMind 的 `index_weights` 数据集就是这么做的） |

### 3.5 依赖可行性（已实测；2026-11-15 逐项复核 PyPI 元数据）

| 检查项 | 结果 |
|---|---|
| akshare 版本 | **1.20.63**（2026-11-14），`requires-python >= 3.11.5`。**下限须钉 `>=1.20.25`**，原因见下方部署风险 |
| TSP backend venv | **Python 3.14.10** ✅ |
| pandas 兼容 | akshare 上游 `uv.lock` 锁 **pandas 3.1.0**（numpy 2.4.6），主 CI 跑 `uv run --locked` → **上游实测组合就是 pandas 3.x**。TSP `uv.lock` 是 **pandas 3.0.3**，akshare 约束 `pandas>=2.2.2` 无上界 → **兼容** |
| pandas 3.0 适配 | akshare 2026-10-30 有两个专门提交：`#7466 上调依赖下限，移除 pandas inplace 写法`（**224 个文件**）、`#7476 移除 pandas 与 Python 已弃用的写法`；并新增 `lowest-deps` CI job 专测依赖下限 |
| polars 冲突 | **无**（akshare 不碰 polars，TSP 的 `polars>=1.44,<1.45` 上界锁不受影响） |
| 原生依赖 wheel | `mini-racer 0.14.1` = **py3 通用 wheel**；`curl-cffi 0.17.0` 有 **cp314** wheel → 不需编译 |
| 维护活跃度 | 最近提交 2026-11-12，10 月底有一波集中修复 |

⚠️ **部署风险（2026-11-15 复核修正）**：

> 🔴 **本节原先的第 1 条风险已失效，且当时的记载本身就是错的。** 原文称 akshare
> `pyproject.toml` 有 `py-mini-racer; platform_system == 'Linux'` + `akracer; == 'Linux'`
> 平台条件依赖，且「akshare 的 uv.lock 里没锁这两个包」。**逐版本查 PyPI 元数据后确认：
> 这个依赖块属于 akshare ≤ 1.20.24，1.20.25（2026-11-04）起已移除**，统一为无标记的
> `mini-racer>=0.13.0`。而我当时声称「已实测 1.20.50」—— 1.20.50 根本没有这些标记，
> 说明那一行**并未真正实测**，是把旧版本/文档里的依赖块当成了当前事实。
> 「已实测」这个标题让未经核实的断言看起来像核实过的，这是比断言本身更该记住的教训。

| 版本 | racer 依赖 | Linux 可用性 |
|---|---|---|
| ≤ 1.20.24（2026-11-03） | `mini-racer>=0.12.4; !=Linux`<br>`py-mini-racer>=0.6.0; ==Linux`<br>`akracer>=0.0.13; ==Linux` | ❌ `py-mini-racer` 0.6.0 **只有 `manylinux1_x86_64`，无 aarch64 wheel** → ARM64 Linux 回落 sdist 源码编译，需 V8 工具链，基本必炸 |
| **≥ 1.20.25（2026-11-04，当前 1.20.63）** | `mini-racer>=0.13.0`（**无平台标记**） | ✅ `mini-racer` 0.14.1 覆盖 `manylinux_2_27` x86_64 + aarch64、`musllinux_1_2` 双架构；`py3-none` 通用标签 + `requires_python>=3.10` → **Python 3.14 双架构、glibc/musl 均可装**，Linux wheel 21.2 MB |

**结论（2026-11-15 决策后适用性收窄）**：akshare 现在只是**财务类啃不动时才启用的可选 extra**（见 §3.1），不再是港股数据的主通道。因此：

- **若最终启用该 extra，必须钉 `akshare>=1.20.25`**，否则解析到旧版本会在 ARM64 Linux 上炸（`py-mini-racer` 0.6.0 无 aarch64 wheel）。
- **若直连能覆盖全部所需接口，则本节整段作废、不引入任何新依赖** —— 这是 PR2 首选结局。行情/K线/指数已实测可直连（§3.1.1），只剩财务三表 / 36 列指标 / 分红 / 人气榜待定。
- `requires-python` 地板抬升（`>=3.11` → `>=3.11.5`）**只在实际引入 akshare 时才发生**。

**PR2 的依赖验证顺序**（先便宜后昂贵）：

1. **先直连试财务类接口**（东财 `datacenter-web.eastmoney.com` 系列）。成功 → 不引入 akshare，本节全部作废。
2. 直连失败才启用 extra：`uv lock` 后在 `ubuntu-latest`（CI 已是此平台）验 `uv sync --frozen --extra <akshare extra>` 装通 —— TSP 的 `uv.lock` 是**跨平台通用锁**（含 `resolution-markers`），解析本身在 mac 上就能验，Linux 只需验安装与导入。
3. **Docker 决策**：`Dockerfile:8` 的 `BACKEND_EXTRAS` 默认空 → extra 不会进默认镜像。需明确是「保持可选、UI 卡片置灰带安装按钮」还是「加进默认 extras」（镜像 +21 MB）。
4. 启用后须在 Linux 容器内**实跑一次**目标接口 —— `mini-racer` 是执行反爬 JS 用的，装得上不等于跑得通。

**安装方式**：optional extra + `availability()` 懒导入，照抄 `backend/app/plugins/baostock/provider.py:127-137`：

```python
def availability() -> tuple[bool, str]:
    try:
        import akshare  # noqa: F401
    except ImportError:
        return False, "依赖 akshare 未安装 (点击卡片安装按钮, 或手动: uv pip install akshare)"
    return True, "无需 Key, 开箱即用"
```

→ akshare 没装只让卡片置灰带安装按钮，**绝不影响 A 股启动**。

### 3.6 港股复权因子：自建链路，及一个被实测推翻的捷径

**结论先行**：港股复权走「东财 F10 分红事件 → 自建乘性因子」，落 `data/adj_factor_hk/all.parquet`，与 A 股 `adj_factor/` 目录隔离但**共用**下游全部纯函数（`_merge_adj_factor_store` / `_align_adj_factor_frame` / `_apply_adj_factor`）。实现在 `app/plugins/eastmoney/hk_dividend.py` + `app/services/kline_sync_hk.py`。

#### 🔴 被推翻的捷径：腾讯 `hkfqkline` 不能当因子源

本文档早期版本（及 `hk.py` 的一条注释）曾记「腾讯 `hkfqkline/get?param=hk00700,day,,,N,qfq` 直接返回前复权价，不需自己拿因子重算」。**实测该结论不成立**，两处都已改正：

- 端点确实返回前复权序列（key 是 `qfqday`；第 6 个 param 字段**必填**，传 `bfq`/`none`/空都回 `code=1 bad params`），原始价得换另一个端点 `appstock/app/kline/kline`（key `day`）。
- 但它是**等差（加性）投影**：`qfq = raw − Σ(未来每股港币分红)`，与本项目 `kline_sync.py:661` 早已记下的 A 股「等差显示投影」同一形态。于是 `qfq/raw = 1 − c/raw` **随价格水平漂移**，不是常数。
- 后果是致命的：按「比值突变」反推除权事件，在**汇丰身上产出 207 个假事件**（噪声比值差中位数 4.76e-4），而腾讯只有约 3 个真事件（1.13e-4）—— 信号被噪声完全淹没。这不是「精度不够」，是**方法本身无效**。
- 等差序列也**无法与本地 `fqt=0` 原始价混用**：要么整条替换价格序列（放弃原始价落库契约），要么就得自己拿因子重算。故选后者。

> 这条否定结论有正面佐证：汇丰最新一次除净（2026-11-05，`每股派美元0.1元(相当于港币0.784812元(计算值))`）在加性偏移序列上独立测得的跳变值**恰好是 0.784**，且在该日归零 —— 与 F10 给出的 0.784812 逐位吻合。两个互不相干的探针对上了，说明「F10 分红额」与「等差投影的偏移量」是同一个数，也就说明等差投影的**差分**可用、**比值**不可用。

因此 §13 原计划的「腾讯 qfq vs 东财 `fqt=1` 两源数值互验」**前提不成立**（等差 vs 乘性不可能数值一致），已从测试矩阵与「PR2 未做」表中撤下，改为上面的锚点断言（hermetic，不需网络）。

#### 数据源：东财 F10 分红报表

| 项 | 值 |
|---|---|
| 主机/路径 | `datacenter-web.eastmoney.com/api/data/v1/get`（**与 push2 封禁池独立**） |
| reportName | `RPT_HKF10_MAIN_DIVBASIC` |
| filter | `(SECUCODE="00700.HK")(IS_BFP="0")` |
| 排序 | `NOTICE_DATE,EX_DIVIDEND_DATE` / `-1,-1` |
| 返回字段 | **仅 12 个**，且**没有任何数值型分红列** |

三个实测要点：

1. **`IS_BFP="0"` 是必需的**：`IS_BFP=1` 是「未派发或宣派股息」占位行（除净日为 null）。加上该过滤，腾讯从 91 行降到 27 行。
2. **金额只存在于 `PLAN_EXPLAIN` 中文文本里**（如 `每股派港币5.3元`），必须解析。`SECUCODE="00700.HK"` 与 `SECURITY_CODE="00700"` 实测**等价**，选前者是为了复用 `hk_fin_secucode()`，不另起一套代码规范化分支。⚠️ 该函数已返回带 `.HK` 的形式，**不能再拼一次** —— 畸形 SECUCODE 返回的是**静默空结果**（`code=9201`），不是报错。
3. **服务端不支持按 `EX_DIVIDEND_DATE` 过滤**（实测 `(EX_DIVIDEND_DATE>='2024-01-01')` 返回 0 行），故无法只拉增量窗口；每标的 1 次请求取全史（`pageSize=200` 单页足够：腾讯 27 行、汇丰 95 行），增量在本地做。

走项目自己的 `EastmoneyClient.datacenter_report`（默认 `source=WEB/client=WEB`）实测正常，**不需要**改成 akshare 用的 `F10/PC`。

#### 方案文本解析：96.98% 覆盖率，及剩下 3% 为什么宁缺勿造

在 32 只跨行业港股、**1159 条真实除权事件**上实测（51 种句式骨架、753 种不同文本）：

| 结果 | 条数 |
|---|---|
| 成功解析出港币数值 | **1124（96.98%）** |
| 外币宣派且无港币折算值 | 23（`每股派美元0.1元(可选择以股代息)` 20 / `每股派人民币0.05元` 3） |
| 分派股份（spin-off）无等值 | 9（`每21股腾讯股份分派1股京东集团A类普通股股份`） |
| 红利认股权证 | 3（权证非送股，且无价值数据） |

失败的 35 条**全部**属于「源数据本身没给港币数值」。这类事件 **warn + 跳过**，绝不当 0 处理 —— 当 0 会产出 `ex_factor=1.0` 的**假因子**，比缺一个事件更难发现。编汇率或编市价同理（CONTRIBUTING §4）。

两个必须钉住的解析陷阱（都有测试）：

- `每股派港币1元(相当于人民币0.9元)` → 必须取 **1.0**，取错会让因子偏小约 8% 且数值看着完全正常；
- `每股派人民币0.5元(汇率人民币0.5元:港币0.55元)(相当于港币0.55元)` → 「汇率」片段里也有「港币+数字」，不能被误命中。

真正的护栏是**两个等值分支都强制要求「港」字**，而不是匹配顺序 —— 变异测试实测把顺序对调，全部真实样本结果不变（顺序只是第二道防线）。

#### 因子口径：与 A 股同构，三处刻意不同

```
ref       = (P − D) / (1 + B)      # 交易所除净公式在「无配股 A=0」下的退化形式
ex_factor = P / ref                # 单次事件的前后比价，不是累计值
```

`P` = **除净日前最后一个交易日**的原始收盘（取自本地 `kline_hk_daily`）；`D` = 每股现金分红（港币）；`B` = 每股送转比例。累计由 `_apply_adj_factor` 的 `cum_prod().over("symbol")` 负责，故这里**只出单次比值**。

与 A 股 `provider.get_adj_factors` 的三处差异，都是港股规则决定的：

| 差异 | A 股 | 港股 | 原因 |
|---|---|---|---|
| 参考价取整 | 四舍五入到 0.01 | **不取整** | A 股要对齐官方涨跌停参考价；港股无涨跌停，且 tick size 按价位分档（0.001–0.5），取整反而要维护一张易腐的档位表 |
| 合理性自检 | 涨跌停幅度自检 | `D/P > 0.9`、`B > 5.0`、`P`/`ref` 非正或**非有限** → 跳过 | 港股无涨跌停可参照，改用比例闸门。NaN 必须挡：它穿过所有 `>`/`<=` 比较（NaN 比较恒 False），产出的 NaN 因子经 `cum_prod` 会污染该日之后**整条**序列 |
| 同日多方案 | — | **先合并再算 ref**（D 求和、B 求和） | `_apply_adj_factor` 按 `(symbol, trade_date)` 去重，不合并会**静默丢掉**一条事件 |

`(可选择以股代息)`（scrip dividend）按**全额现金**处理：现金股息是宣派的法定金额，股东选现金还是选股票是个人取舍，不改变除净日的价格调整。

#### fail closed：三态语义

`load_hk_adj_factors` 返回值**三态**，调用方必须区分：

| 返回 | 语义 | `sync_hk_enriched` 行为 |
|---|---|---|
| `None` | 库不存在/读取失败 = **不知道**有没有除权事件 | 默认 `require_adj_factor=True` → `logger.error` + 返回 0，**不落盘** |
| **空帧** | 库存在但无行 = **确认无**除权事件，原始价即复权价 | 正常计算（`_apply_adj_factor` 对空帧原样返回 raw） |
| 非空帧 | 规范三列 + 明细列 | 正常计算 |

配套两条容易漏的约定：

- `sync_hk_adj_factor` 一轮**全部成功**却无任何事件时，仍要落一个**空库** —— 那是「已同步、确认无事件」的凭据；不落文件，上面的闸门就永远打不开。
- 但**有标的失败时刻意不落**：那轮结果不完整，宁可让 enriched 继续中止，也不能用一个空库谎称数据齐全。单标的失败记 warning + 进 `failed_out` + 继续（一只失败不中断整轮），**绝不**把失败伪装成「无除权事件」。

显式传 `require_adj_factor=False` 可降级用原始价计算，但会 warning 说清代价（跨除净日的 MA/MACD 会失真）。

#### 验证

`tests/test_hk_adj_factor.py` **62 项**，全 hermetic（假 client，不触网），复权数值全部是手算可复核的字面量（CONTRIBUTING:708）。端到端那条钉死了「因子真的作用到 enriched 上」：30 天原始收盘 `101+i`，在第 15 天放 `ex_factor=1.25`，断言前 15 天 `close == raw_close × 0.8`、后 15 天 `close == raw_close`（前复权保持最新价不变）。

**变异检验 17/17 全部被抓到**，另有 3 个**等价变异**如预期存活并记录了原因（解析顺序对调、单独去掉任一道 NaN 防护 —— 都被另一道防线兜住）。脚本 `backend/_scratch/mutate_hk_adj2.py`。

| 变异 | 结果 | | 变异 | 结果 |
|---|---|---|---|---|
| M1 等值分支放开币种限制 | ✅ | | M10 因子取倒数 | ✅ |
| M2 送转符号写反 `1+B→1−B` | ✅ | | M11 解析不出时当 0 | ✅ |
| M3 同日多方案不聚合 | ✅ | | M12 缺库时不 fail closed | ✅ |
| M4 参考价取除净日当天 | ✅ | | M13 三态塌成两态 | ✅ |
| M5 两道 NaN 防护全去掉 | ✅ | | M14 无事件时不落空库 | ✅ |
| M6 不剔除停牌日 | ✅ | | M15 全部失败也落空库 | ✅ |
| M7 现金分红闸门失效 | ✅ | | M16 取数失败不记录 | ✅ |
| M8 送转闸门失效 | ✅ | | M17 自建 client 不关闭 | ✅ |
| M9 去掉权证负向断言 | ✅ | | | |

> M14/M15 是**写测试时才发现的实现漏洞**：最初无事件时不落任何文件，导致「全市场都没有除权事件」这种合法状态下 enriched 会永久 fail closed。测试先于文档暴露了它。

**已知局限**：① 单源（东财 F10），无兜底，已登记进 §3.3；② 3% 的事件拿不到港币数值，复权链在这些日前后会有偏差，每次同步都会 warn；③ ~~未接调度~~ → **PR2e 已接入**（独立 job `hk_daily_sync`，默认关闭），但**部分失败时因子库非空却不完整**这个服务层缺陷仍待决，见 §14「PR2e 完成情况」。

## 4. 存储与隔离：五道闸

| # | 闸 | 说明 |
|---|---|---|
| 1 | **独立港股模块** | 直连方案下不再是「独立插件目录」，而是**现有插件内的独立港股模块**：`plugins/eastmoney/hk.py`、`plugins/tencent/hk.py`、`plugins/sina/hk.py`，**只服务 `.HK`，A 股 symbol 返回空**；共用同插件的 `client.py`（复用限速/熔断/封禁池）。隔离性靠 PR1 的 `assert_symbol_market` 护栏 + 模块级 market 门控保证，不靠目录边界 |
| 2 | **`availability()` 懒导入** | 港股模块导入失败或依赖缺失只置灰卡片，不破坏启动（CONTRIBUTING §4 要求插件加载失败需隔离测试）。**若最终启用 akshare extra，该 extra 缺失同样只置灰财务类卡片** |
| 3 | **独立数据目录** | `instruments_hk` / `kline_hk_daily` / `kline_hk_enriched` / `kline_hk_minute` / `kline_hk_index_daily` / `adj_factor_hk`。现有 **11 处 `kline_daily/**/*.parquet` 递归 glob**（`repository.py:175,177,2808,2809`；`api/kline.py:2240`；`jobs/daily_pipeline.py:1060`）**扫不到港股**。复权因子同理：港股落 `adj_factor_hk/all.parquet`，与 A 股 `adj_factor/all.parquet` **文件名相同、目录不同**，`tests/test_hk_adj_factor.py` 有显式隔离断言 |
| 4 | **DuckDB 视图前缀隔离** | 港股视图统一 `hk_*` 前缀（抄 QuantMind `quanthk_hub.py:45-92`：子类只换数据根 + 视图前缀 `qhk_*`，防跨市场视图串味） |
| 5 | **AST 架构守卫 + 串市场回归测试** | `tests/test_market_architecture_guard.py`（**PR1 已落地**）扫描 `backend/app/` 全部文件，禁止 `market == "HK"` 式字面量分支，命中即报 `file:line`；测试断言「`market=HK` 的任何响应不含 CN 数据」「缓存不串市场」「非法 market 4xx」。<br>*原计划的 `grep -rn "if .*market\s*==\s*['\"]"` 已弃用*：实测全仓仅 3 处命中，其中 **2 处是我们自己 docstring 里引用该反模式作警示**，grep 一上来就被自己的文档绊倒 |

> **为什么不用「market 作二级分区」**：因为上述 11 处递归 glob 会把港股行静默拉进 A 股全量计算 —— 正是 CONTRIBUTING §3 警告的「不报错但结果错」。独立目录是唯一能保证零污染的方案。

**目录路由改造点**：`backend/app/tickflow/repository.py:83-89` 的 `enriched_dirname(asset_type)` / `daily_dirname(asset_type)`（现返回 `{"stock": "kline_daily", "etf": "kline_etf_daily", "index": "kline_index_daily"}`）加 `market` 参数；`repository.py:1717-1727` 的 `resolve_asset_type(symbol)` 同步加 market 判定。

**路径解析必须收敛到一个模块/一个函数，禁止字面量**（QuantMind `quantdb_paths.py:3-6` 教训：硬编码导致便携安装时「读空 → 静默降级」）。**「目录非空才算可用」**。

**`instruments_hk` schema**：沿用现有 13 列（`symbol`/`name`/`code`/`exchange`/`region`/`type`/`listing_date`/`total_shares`/`float_shares`/`tick_size`/`limit_up`/`limit_down`/`as_of`）**+ 新增 `lot_size`（每手股数）、`industry`（恒生行业）、`is_shsc`（沪港通标的）、`is_szsc`（深港通标的）**。

- `limit_up`/`limit_down` 恒 null（港股无涨跌停）
- 同步时遵守 `instrument_sync.py:150-175` 的 `_preserve_instrument_fields` 纪律：**绝不能用 null 覆盖已有值**（缺 `listing_date`/`total_shares`/`float_shares`/`tick_size` 会让 enriched 的 turnover_rate/市值变 null，依赖它们的策略会选不出任何标的）

## 5. 港A差异清单

| 差异 | A 股 | 港股 | 处理 |
|---|---|---|---|
| 时区 | UTC+8 | UTC+8（`Asia/Hong_Kong`） | 同，**无夏令时** ✅ |
| 交易时段 | 9:30-11:30 / 13:00-15:00（240 分钟） | **9:30-12:00 / 13:00-16:00**（330 分钟） | `MarketTradingRules.sessions`；`_TRADING_TOTAL_MINUTES` 不能再用单一常量 |
| **半日市** | 无 | **圣诞前夕 / 农历新年前夕 / 除夕 只交易到 12:00**（无下午盘） | ⚠️ **QuantMind 也没做**（`market_sessions.py:32-35` HK sessions 固定两段）。`sessions` 必须能**按日期变长**，否则半日市下午 `in_continuous_session()` 会误报「盘中」。需要一张半日市日期表 |
| 交易日历 | 上交所 | **XHKG**（复活节 / 圣诞 / 农历，与 A 股完全不同） | 复用现有 oracle 设计（拉港股蓝筹篮子看最新 tick 日期）+ 可选引 `exchange_calendars`(XHKG) 作权威源；判定**带 provenance**（`(bool, source)` 三级：db_override > exchange_calendar > weekday_fallback）；**实盘/告警路径 fail-closed 拒绝 weekday_fallback** |
| 涨跌停 | ±10% / ±20% / ±30% | **无** | `has_price_limit=False` → `price_limit_pct` 返回 `None`；enriched 的 `limit_up_price`/`limit_down_price` 恒 null |
| T+N | T+1 | **T+0**（T+2 交收不影响交易） | 规则表字段 |
| 每手股数 | 固定 100 | **按个股 board lot** | `stock_hk_security_profile_em.每手股数` → `instruments_hk.lot_size`。**TSP 超过 QuantMind 之处**（见 §17） |
| 最小价位 | 固定 0.01 | **按股价分档（spread table）** | `tick_size` 按档位表算，不能固定 0.01 |
| 货币 | CNY | **HKD** | 存储始终本币，仅展示层标 `HK$` |
| **报告货币** | 统一 CNY | **不统一！** 腾讯财报 `CURRENCY=人民币`/`IS_CNY_CODE=0`，另有港元/美元 | ⚠️ **最阴险的坑**：股价是港元、财务数字可能是人民币 → 直接算 PE/PB/市值**会错**。必须用 `CURRENCY`/`IS_CNY_CODE` 显式判定，折算或标注 |
| 行情延时 | 实时 | **15 分钟延时** | 全链路明示；`kline_as_of`（`stock_analyzer.py:430` 已有）加延时属性标注 |
| 费用 | 佣金 + 印花税双边 + 过户费 | 佣金 0.03%（最低 3 HKD）+ 印花税 0.1% **卖出单边** + **无过户费** | 规则表（本期不做交易，仅备将来） |
| A 股独有语义 | 连板 / 涨停 / 情绪周期 / 龙虎榜 | **无** | enriched 对应列（`consecutive_limit_ups`/`signal_limit_up`/`signal_broken_limit_up`）**恒 null 不伪造** |
| 港股独有数据 | — | CCASS 中央结算持股 / 南向资金 / AH 溢价 / **VHSI 恒指波幅** | 板块 tab 第二、三梯队（§10） |
| 标的规模 | ~5000+ | **~2621 只正股**（东财 `m:116+t:3` 实测；`m:116` 全量 17580 含权证牛熊证）。QuantMind `security_master` 为 **2807 只** | 爬取量估算基准 |

## 6. 能力矩阵与插件契约

按 CONTRIBUTING「Capability Routing Matrix Contract」，新增能力必须走完整四步：**registry（`data_providers/capabilities.py`）+ preferences getter + capability-matrix 注入 + matrix 测试**。

- `ProviderCapabilities` 加 market 维度；`CAPABILITY_REGISTRY` 每项声明港股支持度
- 页面门控用 `usable`，**前端不得硬编码能力列表**
- 能力层中立：通用 UI 里不出现供应商分级词表
- 每个能力**独立路由**，不用 `same_as_daily` 这类偷懒声明
- 港股不支持的能力（depth5、概念板块）→ **明确声明不支持 + 前端隐藏 + 说明原因**，不显示假数据

`MarketDataProvider` Protocol 需扩展 `get_instruments`/`get_daily`/`get_adj_factors` 的 market 参数（`data_providers/base.py`，现 `AssetType = Literal["stock","index","etf"]`）。

## 7. API 设计

**market 作显式 query 参数贯穿每个 API**，不用全局拦截器（可审计）。QuantMind `research.py` 的正面样例：

```python
@router.get("/stock-names")   # market: "CN 或 HK；CN 走 instrument 表，HK 走 quanthk security_master"
_HK_STOCK_NAMES_CACHE.get(market)   # ← 缓存 key 带 market，不串市场
```

- 共用端点：加 `market` 参数（默认 `CN`），**缓存 key 必须带 market**
- 港股专属聚合端点：QuantMind 用 `/research/hk/stock-detail`（聚合 CCASS 席位/南向持股/估值/分红/财务/分析师）。TSP 按需决定是否设专属端点，但**页面必须共用**（§8）
- 非法 market → **4xx**，不静默兜底

## 8. 前端设计：共用页面 + 适配器，禁止 fork

QuantMind 两种模式的实证结果决定了路线：

| 模式 | QuantMind 用在哪 | 结果 |
|---|---|---|
| **共享页面 + adapter 传 market** | `electron/src/features/inference-center-shared/`（`adapter.tsx:25` `market: 'CN'\|'HK'\|'US'\|'FUTURES'`；README:42 表格：market 传给 `/models`、`/research/models`、`predict-stock`、模型过滤；calendar 传 SSE/HKEX/NYSE/CME） | ✅ **没出事故**，测试直接 `renderPanel({ market: 'HK', marketLabel: '港股' })` |
| **fork 页面 + 市场专属组件** | `market-analysis{-shared,-hk,-us,-futures}`、`stock-terminal{-shared,-hk,-us}` | ❌ **两起事故**（见下） |

**事故记录**：

- `docs/推理中心三市场_设计方案.md:9-14`：两个页面 **1013 / 1010 行互相复制**，diff 291 行，**双向漂移**，**切到美股渲染的是 A 股页面**
- `docs/首页六宫格多市场_设计方案.md:11-14,36-58`：「**标题改了、口径没改，比不切换更误导**」

且 QuantMind 的 `market-analysis-shared/` 实际只有 4 个文件（`SectorHeatmapChart.tsx`/`ui.tsx`/`StockTerminalSearchBox.tsx`/`StockTerminalWindow.tsx`），**共享层很薄、绝大多数组件是市场专属的**，与其 `marketContent.ts:1-8` 头注释宣称的「市场之间的差异**只写在本文件**，卡片组件里禁止 `if (market === 'HK')`」不符。

**TSP 规则**：

1. **全线共用页面**：个股分析（含多空辩论）、财务概况、复盘、机器人设置——**不新建港股页面**
2. 市场差异**只写在适配器文件**（`marketContent.ts` 式），组件里**禁止 `if (market === 'HK')`**
3. **空态即契约**——空态必须解释 WHY 并给出行动路径。抄 QuantMind `marketContent.ts:100-112` 原文案风格：
   ```ts
   HK: {
     market: 'HK', label: '港股',
     accent: 'bg-orange-50 text-orange-700 border-orange-200',
     source: 'AkShare HK', currency: 'HK$',
     boxes: {
       trade:  { emptyHint: '只统计港股标的（xxxxx.HK）的数据；A 股数据不会混入' },
       market: { emptyHint: '港股数据未就绪，可到「数据源 → 港股」触发同步' },
     },
   }
   ```
4. **市场级开关**：未就绪直接隐藏入口，不显示假数据
5. **数据源标签必须真实**：15 分钟延时、单源无兜底、last-good 快照降级——都要在 UI 上标出来
6. 未知 market 兜底 CN（不白屏），但**过滤类请求不兜底**（§2）

## 9. AI 能力与市场护栏

### 9.1 prompt 市场护栏

新建港股护栏模板，**仅 market=HK 时注入**。照抄 QuantMind `backend/services/engine/routers/ai_ide/skill_templates/hk_market_guardrail.md` 的结构（该文件头注释：「用途：非 A 股市场（港股/美股/加密货币/期货）策略生成时的市场口径护栏。仅市场为 HK/US/CRYPTO/FUTURES 时注入」）：

1. **禁止套用 A 股规则**：T+1 回转、100 股整手、涨跌停 ±10%/±20%、ST 过滤、过户费、印花税卖出单边——**全部不适用于港股**
2. **symbol 口径**：5 位数字 + `.HK`（`00700.HK`）；8 开头创业板保留 5 位
3. **港股 T+0**（当日可卖，T+2 交收不影响交易）；**无涨跌停**；每手股数按个股 board lot（**用 `instruments_hk.lot_size` 真值，不要假设 100**）
4. **费用/货币/基准**：佣金约 0.03%（最低 3 HKD）、印花税 0.1%（卖出单边）、无过户费；**货币 HKD**；默认基准 **HSI**
5. **数据口径**：**行情 15 分钟延时**；**财报货币可能与股价货币不一致**（`CURRENCY` 字段），估值比率需注意口径
6. **`consecutive_limit_ups` / `signal_limit_up` / `signal_broken_limit_up` 等 A 股专属列为 null 是「港股无此语义」，不是「没涨停」——禁止推断**

> 第 6 条是 AI 版的 CONTRIBUTING §3「看起来对但错」：模型会用 A 股常识脑补，把 null 连板数当成「没涨停」。

### 9.2 必须改的现有代码

`backend/app/services/ai_data_quality.py:209,219`：

```python
fin_period = _latest_period_end(fins) if asset_type == "stock" else None   # :209
if asset_type == "stock":                                                   # :219
    issues.extend(_check_financials(fins, fin_period, today))
    issues.extend(_check_minute_gaps(data_dir, symbol))
```

`asset_type == "stock"` 判据会把港股误判成「**缺财务的 A 股**」→ 扣分；开启 `should_block` 时**直接拒绝分析**（`stock_analyzer.py:419` yield `block_message`）。**必须加 market 维度**。这是静默错误，最阴险。

### 9.3 个股分析链路（已核实可行）

`stock_analyzer.py` 的数据依赖很窄，港股完全够用：

- `_KLINE_KEEP_COLS`（:354-366）= 模型实际看到的全部列：OHLCV + change_pct + MA/MACD/KDJ/RSI/BOLL/ATR/量比 + turnover_rate + A 股专属信号列
- `analyze_stock_stream`（:393-435）流程：`_load_kline` → `compute_levels(df)` → `resolve_asset_type` → `_load_financials` → `ai_data_quality.score_symbol` → `should_block` 检查 → `ai_audit.begin_run` → `_clean_rows`
- **已有降级先例**：`:407` `fins = {} if asset_type in {"etf","index"} else _load_financials(...)`
- `indicators/levels.py:566-591` `compute_levels(df)` 产出 11 组关键价位（`sr`/`pivot`/`extreme`/`boll`/`keltner_s|m|l`/`atr_stop`/`gap`/`fib`/`round`）——**纯技术计算，零 A 股耦合**，港股直接可用
- AI 审计（`ai_audit.begin_run`/`link_report`）市场无关，直接复用

### 9.4 多空辩论

**QuantMind 完全没做港股辩论**（`electron/src/features/trading-agents/` 零 market 引用）→ TSP 要做就是超越。

要求：辩论各方 prompt **同样注入市场护栏**，且**多空双方不能用 A 股涨跌停/连板逻辑论证**（例如「已经三连板，情绪到位」这类论证对港股无意义）。

### 9.5 机器人入口

企业微信 / 钉钉智能机器人**共用现有入口**，消息模板增加：市场标识 · 延时标注 · 货币单位（`HK$`）。

## 10. 港股板块 tab

概念板块无源（§3.4），但用**港股真实存在的维度**能撑起一个比 A 股概念板块更有投资价值的独立 tab。

### 第一梯队：恒生行业四件套

数据源 = `stock_hk_company_profile_em` 的 **`所属行业`** 字段，即**恒生行业分类（HSICS），全市场覆盖**。

QuantMind 生产验证：`backend/services/api/market_analysis_hk/quanthk_feed.py:7` 头注释「**板块体系：akshare_profile「所属行业」（恒生行业分类，全市场覆盖），而非 sector/141 只子集**」；`:291`「symbol(0700.HK) -> 所属行业（akshare_profile 全市场恒生行业分类）」。

| # | 功能 | QuantMind 对应 | 内容 |
|---|---|---|---|
| 1 | 恒生行业热力图 | `quanthk_feed.py:313` | 行业平均涨幅 / 成交额 / 领涨龙头（**全市场口径**） |
| 2 | **行业估值温度计** | `:804` + `HkSectorValuationPanel.tsx` | 恒生行业 × **PE 中位数 / 平均股息率 / 成分覆盖**，按股息率降序（组件头注释：「行业估值温度计 —— 恒生行业 × PE 中位数 / 平均股息率（**价值洼地识别**）」）。**港股是红利市场，这个比涨幅排名更有用** |
| 3 | 恒生行业轮动 | `:1266` | 1/5/20 日平均涨幅 + 今日成交额，识别强弱板块切换 |
| 4 | 行业 L2 下钻 | `hk_heatmap_drill.py:1` | 点行业看成分股（「**A 股 /heatmap/drill 模式平移**」） |

⚠️ **必须全市场口径**：QuantMind 早期用 141 只港股通子集，后来返工改全市场。测试断言可抄：`assert len(sectors) >= 15, f"恒生行业数 {len(sectors)} 异常偏少"`（`test_quanthk_market_analysis.py:91-94`，注释「恒生行业热力图（**全市场行业覆盖，非 141 只子集**）」）。

⚠️ **行业排名必须按 market 分开算**：港股行业分类体系（恒生 HSICS）≠ A 股（申万 / 东财），**混在一个维度里排名会产出无意义结果**。

✅ **复用 TSP 现有 ext_data 通道**：TSP 的概念/行业成分**不是内置硬编码**，而是走 ext_data（`rps_rotation.py:97-140` `_load_concept_map_df(repo, kind)`：`ExtConfigStore` + `_dimension_field(config, kind)`，`kind ∈ {"concept","industry"}`，600s 缓存）。港股行业映射落一张 ext_data 表，即可复用 **RPS轮动 / 板块监控（`sector_monitor.py:271 _industry_paths`）/ 主线识别（`market_mainline.py`）/ 板块轮动（`sector_rotation.py:428`）** 全链路。

### 第二梯队：港股独有维度

| # | 功能 | 数据源 |
|---|---|---|
| 5 | 南向资金 / 港股通 | `stock_hk_security_profile_em` 的 `是否沪港通标的` / `是否深港通标的`；`stock_hk_ggt_components_em`（港股通成份） |
| 6 | AH 溢价 | `stock_zh_ah_name_tx` / `_spot_tx` / `_daily_tx` / `_spot_em`（15 分钟延时）。QuantMind 的 `ah_premium` 是三源拼装：A 收盘=本地库、H 收盘=akshare、汇率=中行折算价（`currency_boc_sina`） |
| 7 | CCASS 机构持仓 | **可选，成本高**（QuantMind 为「聪明钱」写了 11 个前端组件 + 6 个测试文件），见 §16 |

### 第三梯队：补充维度

| # | 功能 | 数据源 |
|---|---|---|
| 8 | **VHSI 恒指波幅** | `index_hk_spot_sina`。**港股版 VIX，A 股没有**（iVIX 已停发）→ **替代 A 股的情绪周期维度** |
| 9 | 人气榜 | `stock_hk_hot_rank_em` / `_latest_em` / `_detail_em` / `_detail_realtime_em`（东财股吧港股人气，前 100 名） |
| 10 | 分红日历 | `stock_hk_dividend_payout_em` |

### 概念板块

**前端隐藏 + 明示「港股无概念板块数据源」**，不显示假数据（CONTRIBUTING §4 fail closed）。

> 架构上无需改动：TSP 概念成分走 ext_data 通道，将来若找到港股概念数据源，用户配一张扩展表即可接入，**零代码改动**。

## 11. 指数

`index_hk_spot_sina` 实测返回 **43 个指数**：

- **恒生系列**：`HSI` 恒生指数 · `HSCEI` 恒生中国企业指数（国企指数）· `HSTECH` 恒生科技指数 · `HSCCI` 恒生香港中资企业指数（红筹）· **`VHSI` 恒指波幅指数** · `HSMBI` 恒生中国内地银行 · `HSMPI` 恒生中国内地地产 · `HSMOGI` 恒生中国内地石油及天然气 · `HSCASI`/`HSSCID`/`HSSCPB`/`HSIDI`
- **中证香港系列（9 个）**：`CSHK100` · `CSHKDIV` 红利港币 · `CSHKME` 内地股港元 · `CSHKSE` 内地国企 · `CSHKPE` 内地民企 · `CSHKMCS` 中盘精选港币 · `CSHKLC` 内地消费 · `CSHKLRE` 内地地产 · `CSRHK50` 锐联基本面50港币
- **中华系列（8 个）**：`CES100` 港股通精选100 · `CES120` · `CES280` · `CES300` 沪深港300 · `CESA80` · `CESG10` 博彩业 · `CESHKM` 香港内地 · `CESHKB`
- **标普香港**：`HKL` 大型股 · `GEM` 创业板

⚠️ **坑：该接口混返 A 股指数**（`CSI300`/`SSE50`/`SSE180`/`SSE380`/`SSECOMP`/`SSEDIV`/`SSEMEGA`/`SSEMCAP`/`SSEITOP`/`SSECEQT`/`SSE180GV`/`CSCMC`）→ **必须白名单过滤**，否则港股指数列表里会混进上证综指。

**核心指数（per-market 契约）**：恒生指数 HSI · 恒生科技 HSTECH · 国企指数 HSCEI · 中资企业 HSCCI。

**改造点**：`backend/app/services/index_const.py` 现为**产品级固定契约**（`CORE_INDEX_NAMES = {"000001.SH":"上证指数","399001.SZ":"深证成指","399006.SZ":"创业板指","000680.SH":"科创综指"}`），CONTRIBUTING 明写「4 core indices are a fixed product contract NOT going through the matrix」，且前后端（含 `Layout`）都引用它 → **改为 per-market 定义，并同步更新 CONTRIBUTING 与该文档**。

**存储**：独立目录 `kline_hk_index_daily`，**不混进 `kline_index_daily`**。

**成份权重**：akshare 无恒生成份股接口，可走中证港股通系列（`index_stock_cons_weight_csindex`）。

## 12. 财务概况

**共用 A 股同页面同入口**（用户诉求 3）。

| 接口 | 实测 |
|---|---|
| `stock_financial_hk_report_em(stock, symbol, indicator)` | **三大报表时序**。`symbol` ∈ {资产负债表, 利润表, 现金流量表}；`indicator` ∈ {**年度**, **报告期**}。腾讯（`stock="00700"`）返回 **970 行 × 11 列，2001-12-31 → 2022-12-31**，字段含 `SECUCODE=00700.HK`/`SECURITY_CODE`/`AMOUNT`/`STD_REPORT_DATE` |
| `stock_financial_hk_analysis_indicator_em(symbol, indicator)` | **主要指标全历史**，9 行 × **36 列**，含 `FISCAL_YEAR=12-31`、**`CURRENCY=人民币`**、**`IS_CNY_CODE=0`** |
| `stock_hk_financial_indicator_em` | 核心必读-最新指标（快照） |
| `stock_hk_dividend_payout_em` | 核心必读-分红派息 |
| `stock_hk_growth/valuation/scale_comparison_em` | 行业对比：成长性 / 估值 / 规模 |
| `stock_hk_security_profile_em` | 证券资料 14 列：证券代码/简称/上市日期/证券类型/发行价/发行量（股）/**每手股数**/每股面值/交易所/**板块**/年结日/ISIN/**是否沪港通标的**/是否深港通标的 |
| `stock_hk_company_profile_em` | 公司资料 17 列：公司名称/英文名称/注册地/成立日期/**所属行业**/董事长/公司秘书/员工人数/办公地址/公司网址/E-MAIL/年结日/联系电话/核数师/传真/公司介绍 |

> **修正记录**：本方案早期版本曾判定「港股只有快照式最新指标，无按报告期五表时序，财务分析做不了」——**该判定错误**，已由上述实测推翻。港股有三大报表时序 + 年度/报告期两种粒度。

⚠️ **报告货币陷阱（必须处理）**：港股公司报告货币不统一（腾讯 = 人民币，另有港元 / 美元），而**股价永远是港元** → 直接拿财务数字算 PE/PB/市值**会错**。必须用 `CURRENCY`/`IS_CNY_CODE` 显式判定，折算或标注。

**改造点**：

- `stock_profile.py`（A 股财务概况页，字段如 `free_shares_wan`/`hold_shares`/`each_ballot_shares` 是 A 股形状）→ 加 market 分派，港股映射上述接口
- `financial_sync.py` / `financial_analyzer.py` 的 A 股五表口径 → 港股是**三表**，且需处理报告货币；不适用处 **fail closed**，不静默套 A 股口径
- 遵守 CONTRIBUTING §3.4：turnover_rate 优先用 `announcement_date ≤ 目标交易日` 的历史股本；**维表日期 ≠ 财务公告日 ≠ 报告期**，三者不可混用
- 已有降级先例可参考：`stock_analyzer.py:407` `fins = {} if asset_type in {"etf","index"}`

### 12.1 PR2b 实测：直连 datacenter 成功，**不需要 akshare**

§3.1 的选型是「财务类先试直连东财 datacenter，啃不动才引入 `akshare>=1.20.25` 可选 extra」。**结论：直连啃得动，akshare extra 不引入。** 上面那张 akshare 接口表因此只作为字段语义的参照，不再是取数路径。

直连走 `datacenter-web.eastmoney.com/api/data/v1/get`，在 `client.py` 里有**独立于 push2 的令牌桶 / 熔断 / 封禁池**（实测 push2 被 IP 封禁期间 datacenter 仍正常）。

**报表名（逐个实测，`code=9501 报表配置不存在` 即不存在）**：

| 报表名 | 形态 | 用途 |
|---|---|---|
| `RPT_HKF10_FN_MAININDICATOR` | **宽表**，一行一报告期，76 字段 | 主要指标 + 现金流核心项 + 估值 |
| `RPT_HKF10_FN_INCOME` | **长表**，一行一科目（~27 项/期） | 利润表明细 |
| `RPT_HKF10_FN_BALANCE` | **长表**，一行一科目（~55 项/期） | 资产负债表明细 |

> 🔴 **港股没有独立的现金流量表报表**。试过 `_CASHFLOW` / `_CASHFLOWS` / `_CASH` / `_CF` / `_CASHFLW` / `_CASHFLOWSTATE` 全部 9501；`RPT_HKFINANCE_MAINFINADATA`、`RPT_DMSK_HKFIN_MAIN` 亦不存在。现金流核心项作为**宽表列**存在：`NETCASH_OPERATE` / `NETCASH_INVEST` / `NETCASH_FINANCE` / `END_CASH` / `OCF_SALES` / `PER_NETCASH_OPERATE`。所以港股是「宽表 + 两张长表」，**不是 A 股的四表结构**。

**规模实测（决定取数策略）**：

| 报表 | `REPORT_DATE>='2024-01-01'` | 仅年报且 `>='2025-01-01'` |
|---|---|---|
| MAININDICATOR（宽） | 15,774 行 / **32 页** | 3,312 行 / **7 页** |
| INCOME（长） | 417,063 行 / **835 页** | 88,314 行 / 177 页 |
| BALANCE（长） | 637,825 行 / **1276 页** | 134,749 行 / 270 页 |

→ **宽表走全市场分页**（6 年窗口约 60~70 页即覆盖全部港股），**长表只按标的下钻时取**（全市场同步要上千页，不划算）。

**接口硬限制（都是实测撞出来的）**：

- `pageSize` **硬上限 500**：传 1000/5000 服务端静默截断成 500，且 `pages` 恒按 `count/500` 算 → 若按调用方给的 page_size 判断翻页终止条件会**提前停、静默少取数据**。`fetch_hk_main_indicator` 因此对 `page_size > 500` 直接抛 `ValueError`。
- `in` 批量**上限约 200**：filter 走 query string，n=200（2215 字符）可用，**n=500 → HTTP 414 URI Too Long**。服务层按 200 自动分批。
- filter 语法：**并列即 AND**，`(SECUCODE in ("00700.HK","09988.HK"))`、`(REPORT_DATE>='2024-01-01')`、`(DATE_TYPE_CODE="001")`。🔴 **不支持 `between`** → `code=9501 ... NoViableAltException`，日期区间必须拆成 `>=` 与 `<=` 两条。
- 🔴 **畸形 `SECUCODE` 静默返回空结果（`code=9201`）而非报错**。这是本项目最难定位的一类失败：症状是「同步成功，0 行」。`hk_fin_secucode` 因此对 `00700.HK` 先剥后缀再规范化，杜绝 `00700.HK.HK`。
- `DATE_TYPE_CODE`：`001`=年报、`002`=中报、`003`=一季报、`004`=三季报。

**四个语义陷阱（映射错就是错数据，不是缺数据）**：

| 陷阱 | 事实 | 处理 |
|---|---|---|
| 🔴 **币种双轨且两表表示法不同** | 宽表 `CURRENCY` 是**计价币种**（港币柜台 `HKD`／人民币柜台 `CNY`）；长表 `CURRENCY` 是**中文名**（`"人民币"`），`CURRENCY_CODE` 才是 ISO 编报币种（`CNY`）。腾讯**计价 HKD、编报 CNY** | 映射成**两个刻意不同名**的字段：宽表 → `pricing_currency`，长表 → `report_currency`。API 响应强制带 `currency_warning` |
| 🔴 **港股年结日不统一** | 年报（`DATE_TYPE_CODE=001`）的 `REPORT_DATE` 月份实测**跨 02~12**，是各公司自定的财年结日 | 绝不假定年报 = 12-31，绝不与「上一日历年 12-31」比较 |
| 🔴 **`FISCAL_YEAR` 同名不同义** | A 股 `fiscal_year` 是 **Int64 年份**（2026）；港股东财 `FISCAL_YEAR` 是**字符串 `"12-31"`**（财年结日月-日） | 不进投影表，由 `period_end[:4]` 重新推导成 Int64 |
| 🔴 **周转天数 vs 周转率** | 港股 `INVENTORY_TDAYS` / `ACCOUNTS_RECE_TDAYS` / `TOTAL_ASSETS_TDAYS` 是周转**天数**；A 股 `inventory_turnover` / `receive_account_turnover_ratio` / `total_assets_turnover_ratio` 是周转**率/次数** —— 互为倒数关系而非同值 | 明确排除，不映射。同理排除 `END_CASH`（现金流量表期末现金）≠ `cash_and_equivalents`（资产负债表货币资金）、`EQUITY_MULTIPLIER` ≠ `long_term_debt_equity_ratio`、`ROE_YEARLY`（年化）≠ `ROE_AVG`（加权平均，A 股 `roe` 对应后者） |

**`ORG_TYPE` ∈ {一般企业, 银行, 证券, 保险}** 决定哪些字段族有值：实测工行（01398）`LOAN_ASSETS=61.92` / `DEPOSIT_ASSETS=103.29` / `NET_INTEREST_INCOME=1.227e11`，而腾讯这些字段全 `None`。港股财务 UI **必须按 `org_type` 分支或隐藏空区块**，不能套 A 股的一般企业版式。

**长表科目名是港式 / IFRS 表述，前端不能复用 A 股标签**（实测腾讯 2026-06-30）：`营业额` / `其他营业收入` / `营运收入` / `营运支出` / `毛利` / `销售及分销费用` / `行政开支` / `经营溢利` / `利息收入` / `融资成本` / `应占联营公司溢利` / `除税前溢利` / `税项` / `除税后溢利` / `少数股东损益` / `股东应占溢利` / `每股基本盈利` / `每股摊薄盈利` / `其他全面收益` / `全面收益总额`。`REPORT_TYPE` 也**跨表不可比**：宽表带年份前缀（`"2026年中报"`），长表是裸值（`"中报"`）→ 展示用的 `period_name` 一律由 `DATE_TYPE_CODE` 查表得到。

⭐ **`PER_SHARES` 就是每手股数（board lot size）** —— 这填补了 §3.4 之外的一处已知缺口：`hk_instrument_rows` 的 docstring 原本写着「东财 clist **不提供**每手股数（`f292` 已实测排除），`lot_size` 恒为 `None`，**需由独立源补齐**」。

| 标的 | `PER_SHARES` | 已知真值 | |
|---|---|---|---|
| 00005 汇丰控股 | 400 | 400 | ✅ |
| 00700 腾讯控股 | 100 | 100 | ✅ |
| 01398 工商银行 | 1000 | 1000 | ✅ |
| 09988 阿里巴巴-W | 100 | 100 | ✅ |
| 00388 香港交易所 | 100 | 100 | ✅ |
| 03690 美团-W | 100 | 100 | ✅ |
| 01299 友邦保险 | 200 | 200 | ✅ |
| 00941 中国移动 | 500 | 500 | ✅ |

**8/8 精确匹配**；全市场年报分页可覆盖 **2761 只**，3312 行里只有 3 行缺该字段。实测每手股数分布从 10 到 100000（10/15/20/25/30/40/50/60/80/100/150/200/250/300/400/500/600/660/800/1000/…/100000），**因股而异，不是市场级常量** → `app.market.HK_RULES.lot_size` 保持 `None`，值落在 `instruments_hk.lot_size`，由 `financial_sync_hk.backfill_hk_lot_size` 回填。

> 🔴 配套改动：`lot_size` 必须加进 `instrument_sync_hk._HK_PRESERVE_FIELDS`。东财 clist 不提供该字段、维表重同步会写 `None`，不在保留名单里的话**每次重同步都会抹掉回填结果**，下单取整随即失效。

**`IS_CNY_CODE` 交叉验证了 PR1 的 `is_cny_counter`：3312/3312 一致，0 假阳 0 假阴（100.00%）**。27 只人民币柜台的后缀形态有 `-R` / `-WR` / `-SWR`，以及 **`–R`（U+2013 EN DASH，如「华润啤酒–R」「联想集团–R」）**；`is_cny_counter` 用 `.endswith("R")` 判定，四种形态全部命中。同时实测确认 `pricing_currency == "CNY"` ⟺ `is_cny_code == 1`，3312 行 **0 反例**。

**规范化投影（共用 A 股财务概况页的契约）**：`hk_financials_canonical(df, table)` 把宽表投影成与 A 股 `financials/<table>` **同名同 dtype** 的四张表（`metrics` / `income` / `balance_sheet` / `cash_flow`）。实测 dtype 逐项一致（`fiscal_year`/`report_date_ms`/`period_end_ms` 为 Int64，`symbol`/`period_end`/`announce_date` 为 String），两市场的帧可直接 `pl.concat`。

- 🔴 `announce_date` 与 `report_date_ms` **恒为 null**：港股宽表不提供公告日，**不拿报告期冒充**（CONTRIBUTING §3.4 要求换手率按「公告日不晚于目标交易日」的历史股本对齐，伪造公告日会污染下游）。
- 港股确实没有的 A 股列（15 个周转率 / 现金比率类指标）**不出现在投影结果里**；要让两市场列集完全一致，再经 `align_hk_to_ashare(hk_df, ashare_df)` 按**真实 A 股帧**补 null（用参照帧的 dtype，不是 `Null` 类型，否则 concat 会因类型不匹配报错）。参照帧为空时原样返回 —— 硬编码一份 A 股列名只会与真实 schema 漂移。
- 港股独有列（`name` / `org_type` / `period_name` / `date_type_code` / `pricing_currency` / `lot_size` / `pe_ttm` / `pb_ttm` / `total_market_cap`）附在末尾，是前端渲染分支的依据。
- **源列缺失时补同 dtype 的 null 列，不省略该列**：schema 漂移是真实场景（给 `HK_MAIN_INDICATOR_FIELDS` 加字段后，之前落库的 parquet 没有该列），省略会让前端按列名取值时 KeyError。空帧同样返回完整列集，使读端只需处理一种形态。

**存储：不建 DuckDB 视图。** A 股财务的视图只在 `DataStore._register_views` 注册，而 `financial_sync._refresh_financials_views` 是**只打日志的空实现**，读路径（`get_financial_df`）直接用 polars 读 parquet、完全绕过视图。港股沿用直读，**结构上避免了「两套视图注册表漂移」**（CONTRIBUTING 把「写入后存在已知缓存不一致」列为 PR 拒绝项）。目录为 `financials_hk/{main_indicator,income,balance}/part.parquet`，合并语义：新行覆盖旧行（财报重述），**滚动窗口外的历史保留**，拉取失败或返回 0 行时**保留历史不覆盖**。

## 13. 测试矩阵（per CONTRIBUTING）

| 层 | 测试 |
|---|---|
| **A 股零回归** | 既有全量测试（backend 6088 passed / frontend 856 tests）**全绿即达标**。PR1 的 cn 默认参数是主要保障 |
| **market.py 单测** | 规则表完整性；symbol 判据 Python 与 SQL 两载体**结果一致**；过滤场景未知市场返回 None；推断场景兜底 CN |
| **半日市** | 半日市日期表命中时 `sessions` 变短；半日市下午 `in_continuous_session()` 返回 False |
| **交易日历** | 港股节假日（复活节/圣诞/农历）判定正确；provenance 三级降级；实盘路径拒绝 weekday_fallback |
| **插件隔离**（CONTRIBUTING §4 硬要求） | 港股模块导入失败 / 上游接口变更 / 字段缺失 / 空数据 → `availability()` 返回 False + 提示，**A 股启动与全部 A 股功能不受影响**，不崩、fail closed。*若启用 akshare extra，未安装时同样只置灰财务类卡片* |
| **港股列表纯净性** | 断言 `fs=m:128 t:3` 取回的列表**不含窝轮/牛熊证代码段**（实测 `m:116` 会混入 17580 条含衍生品）；断言 total 落在预期量级（主板 ~2621 / 全市场 ~4748），数量级突变即报警 |
| **双柜台处理** | 钉住 §3.1.1 已定策略：`89988`/`09988` **都入库**且 `is_cny_counter` 标记正确；成交额合计/涨跌家数/量比分母/行业排名**只计港币柜台**；按 `89988` 搜索能命中、个股页能出行情 |
| **复权正确性** | 因子链用**固定样本数值断言**（CONTRIBUTING:708）：锚点是汇丰 2026-11-05 实测除净 `每股派美元0.1元(相当于港币0.784812元(计算值))`，其 0.784812 与腾讯 `hkfqkline` 前复权序列在同一日独立测得的加性偏移**逐位吻合**；端到端断言 enriched 的 `close` 已复权而 `raw_close` 未复权；取不到复权数据时 **fail closed**，绝不静默回落 raw 价。⚠️ 原计划的「腾讯 qfq vs 东财 `fqt=1` 两源数值互验」**前提不成立**（等差 vs 乘性），已撤下，见 §3.6 |
| **串市场护栏** | `market=HK` 的任何响应**不含 CN 数据**；非法 market → 4xx；**缓存不串市场**（第二个市场读到第一个市场的序列 = 失败）；11 处 A 股 glob **扫不到港股目录** |
| **CI grep** | `grep -rn "if .*market\s*==\s*['\"]" backend/` 命中数为 **0**（白名单除外） |
| **能力矩阵** | matrix 测试覆盖港股新能力；`usable` 门控生效；不支持的能力（depth5/概念板块）明确声明 |
| **复权一致性** | 缺复权因子时 fail closed，不静默用 raw 价；前复权序列事件间漂移检查 |
| **报告货币** | `CURRENCY != HKD` 的标的，估值比率有折算或标注，不静默混算 |
| **AI 数据体检** | 港股标的**不被误判**为「缺财务的 A 股」；`should_block` 不因 market 误触发 |
| **AI 护栏** | 港股 prompt 含护栏 6 条；模型不把 null 连板数当「没涨停」（可用固定 fixture 断言 prompt 内容） |
| **行业不混排** | 港股行业排名与 A 股行业排名**分开计算**，结果不互相污染 |
| **前端** | 共用页面在 `market=HK` 下渲染港股数据；空态文案正确；延时/货币标注存在；市场开关未就绪时入口隐藏 |

## 14. 分期与工作量（7 个 PR）

**PR1 → PR2 → PR3 是硬依赖链；PR4 / PR5 / PR6 / PR7 在 PR3 之后可并行或任意排序。每个 PR 对 A 股影响均为零。**

| PR | 内容 | 关键交付 |
|---|---|---|
| **PR1** | **地基** | `backend/app/market.py`（`Market` 枚举 + `MarketTradingRules` frozen dataclass + CN/HK 规则表 + `rules_for`/`infer_market`/symbol 判据双载体）；`market_time`/`trading_day`/`price_limits` 三模块 market 分派（cn 默认 → **52 个消费方零改动**）；**半日市支持**；护栏（港股代码进 A 股层当场报错）；CI grep 规则 |
| **PR2** | **数据通路** | **扩现有 `eastmoney`/`sina`/`tencent` 插件直连港股**（`symbol_to_secid` 加 `.HK→116.` 纯增量分支）；列表用 `fs=m:128 t:3`（**不是 `m:116`**，后者混窝轮牛熊证）；**双柜台：两个代码都入库 + `is_cny_counter` 标记，市场级统计只计港币柜台**；独立目录 + `repository.py:83-89` 加 market 参数；**DuckDB 视图 `hk_*` 前缀**；能力矩阵加 market 维（四步全套）；`instruments_hk`（含 **lot_size** / 上市日期 / tick_size / 沪深港通标记 / 恒生行业）；插件隔离测试；**财务类先试直连东财 datacenter，啃不动才引入 `akshare>=1.20.25` 可选 extra**（见 §3.5） |
| **PR3** | **只读看盘 + 指数** | `index_const.py` 改 per-market（+ CONTRIBUTING 同步）；**43 指数白名单过滤**；前端共享层 + `marketContent.ts`；K线 / 自选 / 详情；**15 分钟延时标注**；空态契约文案 |
| **PR4** | **AI 能力** | enriched 通用指标子集（A 股专属列恒 null）；**`hk_market_guardrail`**；`ai_data_quality.py:209,219` 加 market 维；个股分析 + **多空辩论**共用页面；企微 / 钉钉机器人入口 |
| **PR5** | **财务概况** | 三大报表时序 + 36 列指标；**报告货币折算/标注**；共用 A 股同页面同入口；`stock_profile`/`financial_analyzer` 港股口径适配 |
| **PR6** | **港股板块 tab** | 恒生行业四件套（热力图 / 估值温度计 / 轮动 / L2 下钻），**全市场口径**，走 ext_data 通道，**行业排名按 market 分开** |
| **PR7** | **港股独有维度** | 南向 / 港股通 + AH 溢价 + VHSI + 人气榜 + 分红日历 |

### PR1 完成情况（地基）

**状态：已完成，未提交。** 后端全量 **6334 passed / 8 skipped / 1 xfailed / 0 failed**（基线 6088 + 新增 246），存量 A 股测试**一行未改**即全绿 —— 这就是「A 股零影响」的证明方式。Ruff 新增告警 0（剩余 4 处与 HEAD 基线逐条一致，均为存量）。

落地文件：

| 文件 | 变更 |
|---|---|
| `backend/app/market.py` | 新增。`Market`(StrEnum) + `MarketTradingRules`(frozen) + CN/HK 规则表 + `rules_for`/`parse_market`/`infer_market`/symbol 判据双载体 + `assert_symbol_market` 护栏 + 半日市注册接口 |
| `backend/app/market_time.py` | 重写为按 market 取规则；保留 5 个旧常量与全部 12 个导出名；**不做隐式时区转换** |
| `backend/app/services/trading_day.py` | 缓存与单飞按 market 分桶；探测链改为注册表（`_PROBE_CHAIN_NAMES`）；`market` 为**仅关键字**参数，默认 CN |
| `backend/app/price_limits.py` | `price_limit_pct`/`polars_price_limit_pct` 加 `market` 参数（默认 CN）；无涨跌停市场返回 `None`/null；A 股专属函数入口加护栏 |
| `backend/tests/test_market.py` | 新增 108 例（含**双载体一致性**对真实 DuckDB 表校验、instruments 全量回归） |
| `backend/tests/test_market_time.py` | 新增 86 例（CN/HK 时段与已用分钟数逐边界钉死） |
| `backend/tests/test_market_architecture_guard.py` | 新增 9 例（AST 架构守卫 + 守卫自检 + 误报检查） |
| `backend/tests/test_trading_day.py` | +30 例（多市场隔离、并发单飞、探测链组成、tickflow 篮子/时区/缓冲窗） |
| `backend/tests/test_price_limits.py` | +13 例（港股 null 语义、护栏顺序、numpy 混合市场、A 股三段日期语义零回归） |
| `CONTRIBUTING.md` | §2.3 加 `app/market.py` 边界行；§3.3 改为按市场取规则；**新增 §3.5 市场维度契约**；§4 加规范 symbol 形；§9 加市场维度验证行 |

与原方案的偏差（均为实施中发现的更优解或真实缺陷）：

1. **「CI grep 规则」改为 AST 测试。** grep 会命中 `market.py` 与 `trading_day.py` 里**引用该反模式作为警示**的 docstring，一上来就被自己的文档绊倒。AST 只看真实 `Compare` 节点，天然跳过注释/字符串，且 pytest 已在 CI 里跑，无需改 workflow。守卫自带自检（喂合成违规源码）与误报检查，注入 3 种违规写法实测全部命中并给出 `file:line`。
2. **`assert_symbol_market` 对格式宽松，只拦「确实匹配到别的市场」。** 原设计「不匹配声明市场就报错」打断了存量路径：仓内大量内部路径用裸 6 位代码（`"000001"`/`"300001"`），且与带后缀代码**同列混用**（`test_st_limit_and_sharpe.py` 即是），`board_limit_pct → numpy_limit_pct_vectors → write_numpy_price_limit_matrix → build_market_data_matrix` 整条回测链会炸。格式校验是 `is_valid_symbol` 在边界上的职责，规范化是插件层的职责（§4），三者分工不同。已知盲区（裸的他市场代码如 `"00700"`）已写入 docstring 与 §3.5。
3. **半日市表出厂为空**（机制完整、无日期）。风险不对称：漏标半日市 → 退化为全日市，量比低估约 2.2×（330/150）但不丢数据、补上日期即自愈；误标全日市为半日市 → **整个下午盘丢失且不可恢复**。HKEX 每年公告、无免费 API，且部分为农历日无法计算。**后果需知悉：登记日期前，港股每年约 3 个半日市的量比会被低估。**
4. **`Market` 用 `StrEnum` 而非 `(str, Enum)`。** 后者 `str(Market.CN)` 与 f-string 都得 `"Market.CN"`，导致 `f"{market}" == "CN"` 恒假、`cache[f"{market}"]` 与 `cache["CN"]` 变成两个键 —— 不报错，只让分支静默失效。与 QuantMind 印花税静默归零同形。仓库已有 `StrEnum` 先例（`app/tickflow/capabilities.py`），且 `Market(str, Enum)` 原本是全仓唯一一处。

实施中**发现并修复**的既有缺陷（都不是港股新增功能，是 A 股路径上本来就错的）：

| 缺陷 | 后果 | 发现方式 |
|---|---|---|
| `trading_day._CACHE` 是单个模块级实例 | 一旦有港股探针，CN 先探得 True 则同日 HK 直接读走 CN 结论。CN/HK 节假日不重合 → 港股假日空转轮询，或把交易日误判为休市而**整天不拉行情** | 代码审查；已用变异检验（共用桶 → 5 例失败） |
| 单飞键 `(now.date(), epoch)` 不含 market | 同日两市场共用条目，后到者拿走先到者的结论 | 代码审查；已用变异检验（去 market → 并发用例 `BrokenBarrierError`） |
| `is_valid_symbol` 做了 `.strip()` 而 SQL 不做 | `"600519.SH "` 在 Python 合法、在 DuckDB 非法 → 双载体漂移 | **新增的双载体一致性测试**对真实 DuckDB 表跑出 3 处不一致 |
| `.match()` 配 `$` 锚 | Python 的 `$` 还匹配「结尾换行前」，DuckDB RE2 不匹配 → `"600519.SH\n"` 同上漂移 | 同上；已全量改 `fullmatch`（7 处）并加显式回归 |
| `pl.lit(None, dtype=Float64)` 是长度 1 标量 | `with_columns` 里会广播所以看不出问题，调用方一改 `select` 就静默只出 1 行（`test_price_limits.py` 正是 `select` 写法，说明是活的调用形态） | 验证脚本；已用变异检验（还原裸 `pl.lit` → 用例失败） |
| `price_limit_pct` 护栏在 `has_price_limit` 提前返回**之后** | A 股代码配 `market="HK"` 静默返回 `None` → 涨停价算不出、涨停判定静默失效、无报错 | 实测；已用变异检验（挪回顺序 → 用例失败） |
| `rules_for` docstring 称「None (= CN)」但实现是抛错 | 文档与实现矛盾。**改的是 docstring 不是代码** —— 静默默认 CN 会用 A 股规则算港股且无任何信号 | 代码审查 |

三处关键修复都做了**变异检验**（故意还原 bug，确认测试真的失败），避免「测试通过但抓不住东西」。

**PR1 未做（按方案属于后续 PR）**：数据通路、能力矩阵 market 维、前端、AI 护栏、财务、板块 tab、指数。当前 `market` 参数全链路默认 CN，港股仅规则表与护栏就位，**尚无任何港股数据入口**。

### PR2 完成情况（数据通路 + 财务）

**状态：PR2a / PR2b / PR2c / PR2d 已提交（`3996ac5a` + `04d42888`）；PR2e（调度接线）代码与测试已完成，未提交。** 后端全量 **6920 passed / 8 skipped / 1 xfailed / 0 failed**（PR1 后基线 6586 → PR2a/b 6787 → PR2c 6807 → PR2d 6869 → PR2e 6920，新增 51 项与实测增量精确吻合），存量 A 股测试**一行未改**即全绿。Ruff 新增告警 **0**（`pre-existing: 670` 与 HEAD 基线逐条一致）。9 个港股测试文件共 **566 项**，全部离线可跑（无网络依赖）。

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/test_hk_eastmoney.py` | 154 | 插件层：secid / 列表 fs / 双柜台 / tick_size 11 档 / spot 与 kline 映射 |
| `tests/test_hk_financial_sync.py` | 96 | 财务服务层：合并语义 / 规范化投影 / align / lot_size 回填 |
| `tests/test_hk_financials.py` | 74 | 财务插件层：报表名 / filter 语法 / 分页上限 / 字段映射（**已做变异检验**） |
| `tests/test_hk_api.py` | 52 | 10 条路由的契约、参数校验、串市场护栏、enriched 阶段接线 |
| `tests/test_hk_storage_layout.py` | 27 | 五道闸：目录隔离 / 视图不串市场 / `_DIRNAMES` 无 etf |
| `tests/test_hk_kline_sync.py` | 34 | 日线同步：universe / 增量 / `fqt != 0` 拒绝；**enriched 生成**：换手率单位 / 大涨不判涨停 / 限价恒 null / 目录隔离 / 拒绝降级 / 进度 |
| `tests/test_hk_instrument_sync.py` | 16 | 维表同步：字段补全 / `_MIN_FRESH_RATIO` / 保留字段 |
| `tests/test_hk_adj_factor.py` | 62 | **复权因子链**：方案文本解析（11 种真实句式 + 9 种不可解析 + 4 个具名陷阱）/ 因子数值与四道闸门 / `load_hk_adj_factors` 三态 / fail-closed / 端到端复权 vs 原始 / 同步语义（**变异检验 17/17**） |
| `tests/test_hk_scheduler.py` | 51 | **调度接线**：单向让路 / 不进 `job_store` 不占执行槽 / 因子部分失败跳过 enriched / 阶段依赖与软失败 / client 必定 close / 视图必定重建 / 注册与 `replace_existing` / `start_scheduler` 隔离、settings API 契约（**变异检验 28/28**） |

**PR2a（数据通路）交付**：`app/plugins/eastmoney/hk.py`（467 行，数据边界）、`app/services/instrument_sync_hk.py`、`app/services/kline_sync_hk.py`、`app/api/hk.py`、`repository.py` 的 `_DIRNAMES` / `daily_dirname()` / `enriched_dirname()` / `append_hk_daily` / `append_hk_enriched`、`provider.symbol_to_secid` 的 `.HK→116.` 纯增量分支。

> ⭐ **架构护栏守住了**：`tests/test_market_architecture_guard.py::test_no_market_literal_branches_in_app` 扫描 `app/` 里 `market` 与市场字面量的比较，全程 **0 命中**。所有市场差异都走 `_DIRNAMES` / `daily_dirname()` / `enriched_dirname()` / `rules_for()` 查表，没有一处 `if market == Market.HK`。该测试的 docstring 明确写着正确修法**不是**加白名单而是把差异移进 `MarketTradingRules` / 查表 —— 本次照做。

**PR2b（财务）交付**：`app/plugins/eastmoney/hk_financials.py`（685 行）、`app/services/financial_sync_hk.py`（582 行）、`app/api/hk.py` 的 5 条财务路由（共 9 条）。实测结论与四个语义陷阱见 **§12.1**。

**PR2b 实施中发现并修复的 4 个真实缺陷**（都不是「测试写错了」，是写测试时逼出来的实现 bug）：

| 缺陷 | 后果 | 修法 |
|---|---|---|
| `HK_ONLY_CANONICAL_FIELDS` 循环用 `if extra in df.columns` 才 append | 源列缺失时**静默丢掉该列**，而同函数里主投影循环却正确补 `pl.lit(None, dtype)` —— 两条路径不一致。schema 漂移是真实场景（给映射表加字段后旧 parquet 没有该列） | 新增 `HK_ONLY_CANONICAL_DTYPES`，缺失时补**同 dtype 的 null 列** |
| `if df.is_empty(): return pl.DataFrame()` | 空帧是 **0 列**，「列集恒定」的契约只对非空帧成立 → 读端要处理两种形态 | 返回 `pl.DataFrame(schema=hk_canonical_schema(table))`，并新增公开的 `hk_canonical_schema()` |
| `hk_latest_financials(data_dir, "00700")` 直接用裸字符串过滤 | 库里存的是 `00700.HK` → **静默返回空**。与「畸形 SECUCODE 静默空结果」同一类失败模式，最难定位 | 入口先经 `hk_fin_secucode` 规范化，`ValueError` 兜底 |
| `backfill_hk_lot_size` 遇维表缺 `lot_size` 列时返回 0 且不打日志 | 静默 no-op；「缺每手股数」要等到下单取整时才暴露 | 补列 + WARNING（`instruments_hk 缺 lot_size 列, 补列后回填`） |

配套把 `lot_size` 加进 `instrument_sync_hk._HK_PRESERVE_FIELDS`，并**更新了 PR2a 那条断言它「不在名单里」的测试** —— 该断言的前提（clist 不提供每手股数、该列恒 None）已被 `PER_SHARES` 的发现推翻。同时补了行为级测试 `test_backfilled_lot_size_survives_instrument_resync`，两者都经**变异检验**（从名单移除 `lot_size` → 2 项失败）。

> 🔴 **dtype 常量放在服务层而非插件层**：`HK_ONLY_CANONICAL_DTYPES` / `_HK_CANONICAL_KEY_DTYPES` 定义在 `financial_sync_hk.py`，`hk_financials.py` 保持**不依赖 polars**。dtype 是存储关注点，不是数据边界关注点。

### PR2c 完成情况（港股 enriched 生成 —— 补上 PR2a 最后一个功能缺口）

**状态：代码与测试完成，未提交。** PR2a 把 `append_hk_enriched` / `kline_hk_enriched` 视图 / `_DIRNAMES` 全都建好了，但**没有任何调用方**，目录恒空 —— §3.1.2 的换手率单位修复在实践中根本走不到。本节把它接通。

**交付**：

| 位置 | 内容 |
|---|---|
| `app/services/kline_sync_hk.py` | `sync_hk_enriched()`（批量重算，形状对齐 A 股 `recompute_daily_symbols`）、`hk_enriched_instruments()`、`HK_ENRICHED_BATCH_SIZE=200` |
| `app/indicators/pipeline.py` | `compute_enriched(..., *, market=Market.CN)` —— **keyword-only 且默认 CN，8 个存量调用方零改动** |
| `app/tickflow/repository.py` | `daily_repair_files(asset_type, *, market=Market.CN)` |
| `app/api/hk.py` | `POST /api/hk/sync_enriched`（独立重算）+ `HkDailySyncRequest.enriched=True`（日K 同步后自动接续） |

**实施中发现并修复的 3 个真实缺陷**（全部由「先写测试再跑」逼出来，不是读代码读出来的）：

| 缺陷 | 后果 | 修法 |
|---|---|---|
| 🔴 `compute_enriched` 没有 `market` 参数，内部调 `compute_all(...)` 时不传 → 静默按 CN 算 | 港股换手率**高 100 倍**（`shares_per_volume_unit` CN=100 / HK=1）。这正是 §3.1.2 警告的场景，而 `compute_enriched` 恰恰是任何 HK enriched 生成器最自然会调的入口 | 加 keyword-only `market`，透传给 `compute_all` |
| 🔴 `_attach_no_limit_window` 无条件调用，而 `price_limits.no_limit_window_days` 对非 A 股 symbol **硬抛** `SymbolMarketMismatchError` | 港股 enriched 一算就崩。护栏本身是**对的**（其 docstring 明说「注册制新股无涨跌幅窗口是 A 股专属概念，港股本来就无涨跌停，不存在窗口」），错的是调用方 | 把「A 股专属」这个知识收进 `_attach_no_limit_window` 自身：`if not rules_for(market).has_price_limit: return inst_subset`。判定放函数内而非各调用方，是为了让将来新增的调用方**无从忘记** |
| 🟠 `polars_price_limit_pct(...)` 在**批量路径**（`compute_limit_signals`）和**实时路径**（`_compute_limit_signals_today`）都没传 `market` | PR2a 已给这两个函数加了 `market` 参数，但只接了换手率那一半，限价那一半没接 → 会撞上 `price_limits.py:89` 的第二道护栏。不修的话港股「批量能跑、实时崩」 | 两处都补 `market=market`。PR1 早已把机制建好（`has_price_limit=False` 时返回恒 null 的 Float64 表达式，下游 `polars_limit_price` 自然产出 null 限价），只差透传 |

> ⭐ **架构护栏仍然 0 命中**：以上三处修法全部经 `rules_for(market).has_price_limit` **查规则表**，没有一处 `if market == Market.HK`。`tests/test_market_architecture_guard.py` 的 docstring 明确要求「正确修法不是加白名单，而是把差异移进 `MarketTradingRules`」—— 本次照做。

**一个刻意的设计决定：维表缺失时明确拒绝，而不是降级产出。**

`compute_limit_signals` 在 `instruments` 为空时**提前返回**，落盘分区里连 `turnover_rate` 这一列都不存在。实测 DuckDB 行为分两种：

- **所有**分区都缺该列 → 视图里没有这个列，消费方 `SELECT turnover_rate` 直接 `BinderException`；
- **部分**分区缺 → `read_parquet` glob 按名合并 schema，缺的读出为 `NULL`。

同一张表两种行为，属于 CONTRIBUTING:825 的「写入后存在已知缓存不一致」（PR 拒绝项）。加之港股无涨跌停，`compute_limit_signals` 对 HK **只有换手率这一项产出有意义**（其自身 docstring 原话），缺它则产出无价值。故 `sync_hk_enriched` 在维表为空时 `logger.error` 并返回 0、不落盘。注：港股日K 本就依赖维表（`hk_daily_universe` 从它取标的），走到这一步说明维表被删/损坏，属异常状态，该报错而非静默降级。

**一处诚实的近似（已写进 `sync_hk_enriched` docstring，不是遗漏）**：

1. **换手率对全部历史使用当前 `float_shares`**（`historical_shares=None`）：CONTRIBUTING §3.4 要求优先使用「公告日不晚于目标交易日」的历史股本。港股全流通（实测 `f38 == f39`），误差比 A 股小但非零。

> 原先这里的第 1 条「**不做复权**（`factors=None`），后果是 MA/MACD 在除息日附近会失真」**已消除**：因子源已接线，`sync_hk_enriched` 现在传真实因子并默认 `require_adj_factor=True`（缺库即中止），详见 §3.6。

> 港股 `limit_up_price` / `limit_down_price` 恒 null、`consecutive_limit_ups/downs` 恒 0 是**正确结果**，不是缺数据 —— 港股无涨跌停。测试里对此有显式断言，避免后人误当 bug 去「修」。

**变异检验（8 个变异全部被抓到）**：

按 CONTRIBUTING 的纪律，新写的断言逐个做变异检验 —— 注入 bug 后测试必须变红，否则断言只是装饰。每次变异前都 `assert` 原文**唯一命中**，防止 `str.replace` 静默 no-op（本轮 M1 第一次就因搜索串与真实源码不符而命中 0 次，被该 `assert` 拦下；文档里的用例计数也曾因一条写坏的 shell heredoc 静默未生效，同样靠复核才发现）。

| 变异 | 结果 |
|---|---|
| M1 `compute_enriched` 不透传 `market` | ✅ 10 failed |
| M2 去掉 `_attach_no_limit_window` 的 `has_price_limit` 短路 | ✅ 10 failed |
| M3 去掉维表缺失的中止分支 | ✅ 1 failed |
| M4 批量路径 `polars_price_limit_pct` 退回 CN 默认 | ✅ 1 failed（**补测试后**，见下） |
| M5 enriched 误写 A 股目录 | ✅ 8 failed |
| M6 实时路径 `polars_price_limit_pct` 退回 CN 默认 | ✅ 1 failed（**补测试后**） |
| M7 实时路径 `_attach_no_limit_window` 退回无条件调用 | ✅ 2 failed |
| M8 实时路径换手率退回 CN 系数 | ✅ 1 failed |

> 🔴 **M4 第一轮漏网，暴露一个真实测试缺口。** 删掉批量路径的 `market=market` 后，港股 enriched 的 **121 项测试全绿**。原因是双重的：① `polars_price_limit_pct` 是**表达式级**函数，无法逐行抛错，漏传 `market` 不报错，而是静默按 A 股主板 10% 构造表达式（`00700.HK` 不匹配 300/301/302/688/689/.BJ 任何前缀 → 落默认 10%）；② `limit_up_price` **不在 `ENRICHED_STORAGE_COLS` 里**，落盘根本看不出来，只有 `consecutive_limit_ups` 会露馅 —— 而原先的测试数据日涨幅仅 ~1%，永远触发不了 10% 阈值。
>
> 补法：`test_enriched_hk_big_move_is_not_flagged_as_limit_up` 用连续 **+30%** 大涨，同时断言落盘口径（连板数全 0）与内存口径（`limit_up_price` / `limit_down_price` 恒 null）。M6 同理：在 `tests/test_limit_signals_no_price_limit.py`（该文件本就同时覆盖批量与实时两条路径，是「无涨跌停」语义的自然归属）补 `test_hk_big_move_not_limit_up_on_both_paths` 与 `test_hk_realtime_turnover_uses_share_unit`，+2 项（14 → 16）。
>
> 普适教训：**「某列不落盘」+「表达式级函数不抛错」= 静默错误的完美藏身处**。凡修市场差异，都要先问「这个错误数字最终会从哪个可观测字段露出来」，再让测试数据真的能触发它。

### PR2d 完成情况（港股复权因子接线 —— 消除 PR2c 登记的第 1 处近似）

**交付**：`app/plugins/eastmoney/hk_dividend.py`（新增，东财 F10 分红事件 → 乘性因子）、`app/services/kline_sync_hk.py` 的 `HK_ADJ_FACTOR_RELPATH` / `hk_adj_factor_path()` / `load_hk_adj_factors()` / `sync_hk_adj_factor()` 与 `sync_hk_enriched(require_adj_factor=True)`、`repository.py` 三处接线（目录 / 视图 / glob）。

数据源实测、等差 qfq 的否定结论、解析覆盖率、因子口径与变异检验 **17/17** 全部在 **§3.6**，此处不重复。

**PR2d 实施中发现并修复的 3 个真实缺陷**（同样是写测试逼出来的实现 bug，不是「测试写错了」）：

| 缺陷 | 后果 | 修法 |
|---|---|---|
| `build_hk_adj_factors` 对本地收盘价直接 `float(c)` | 停牌/数据缺口日的 close 为 `None` → `TypeError` **炸掉整轮同步**（一只标的一个空值毁掉全市场） | 建索引时先滤掉 `None`。这同时**更正确**：`bisect_left` 会落到更早的**有效**交易日，那才是真正的「除净日前最后一个交易日」 |
| 闸门只判 `p <= 0.0` / `ref <= 0.0` | NaN 穿过所有比较（NaN 参与 `>`/`<=` 恒为 `False`）→ 产出 NaN 因子，经 `cum_prod` 污染该日之后**整条**序列，比丢一个事件严重得多 | 两道闸门都改成 `not math.isfinite(x) or x <= 0.0`。两道**单独看都冗余**（各自能挡住对方漏掉的那一半），是刻意的纵深防御，已作为等价变异 E2/E3 记录在案 |
| 无事件时 `if not frames: continue` 跳过合并 | 因子库文件**根本不创建** → `sync_hk_enriched` 的 fail-closed 闸门在「全市场都不分红」这种**合法**状态下永远打不开 | 一轮全部成功且文件不存在时，落一个**空库**当「已同步、确认无事件」的凭据。🔴 但**有标的失败时刻意不落** —— 空库等于宣称数据齐全，不能拿它骗过闸门 |

> 第 3 条是 `test_sync_no_events_writes_empty_store` 变红才暴露的。它说明 fail-closed 设计有个容易漏的对偶面：**「拒绝在数据不全时出数」与「允许在确认无数据时出数」必须同时成立**，只做前者会把一个合法状态永久锁死。

### PR2e 完成情况（港股同步接入调度）

**交付**：`app/jobs/hk_pipeline.py`（新增）、`preferences.get/set_hk_sync_schedule()`、`PUT /api/settings/preferences/hk-sync-schedule`、`start_scheduler` 中一处隔离注册。测试 `tests/test_hk_scheduler.py` **51 项**，变异检验 **28/28**。

#### 决策：独立 job，而不是并入盘前/盘后

方案原本留的是「待决策」。选**独立 job**，理由是「不能危及 A 股关键路径」这条要求在本仓库里有个**不显然的坑** —— `pipeline_jobs` 的两道并发闸都是**全局**的：

| 闸 | 语义 | 港股若复用会怎样 |
|---|---|---|
| `job_store.create()` 单飞 | 已有 pending/running 任务时返回 `is_new=False`，调用方跳过本轮 | 港股任务一旦登记，**A 股盘后管道会误判「已有活跃任务」而整轮跳过** |
| `try_acquire_run_slot()` 执行槽 | 全局互斥、非阻塞抢占 | 港股持槽期间 A 股抢不到 → 同样被跳过 |

也就是说，「顺手复用 `_run_tracked`」这个最自然的写法，恰好会造成方案明令禁止的后果。因此港股 job 刻意：

- **不进 `job_store`**、**不占执行槽** —— 对 A 股完全不可见，A 股的单飞判定永远不会因港股触发。代价是港股进度不出现在 `/api/pipeline/jobs`，只进日志。
- **单向让路** —— 港股开跑前只读 `job_store.active_id()`，有活跃任务就本轮跳过。让路只发生在港股这一侧。
- **异常全吞** —— 任何失败只记日志，绝不向 APScheduler 上抛；`start_scheduler` 里的注册调用也整体兜底，港股注册失败不影响 A 股调度启动。

并发写盘本身是安全的：港股四张表与 A 股目录物理隔离（闸3），`rebuild_views()` 内部走 `_ddl_lock`(RLock)，共享的 `EastmoneyClient` 令牌桶按 host 统一排队（是保护而非危害），且默认时刻 16:40 HKT 与 A 股盘后 15:35 本就错开。

#### 默认关闭，且刻意不设时间下限

- **默认 `enabled=False`**：港股是增量功能，且维表口径 `FS_HK_EQUITY` 的各 `t:` 子类语义仍待联网复测（见下表「待复测」）。默认开启会让每天 ~2900 标的的拉取在口径确认前就跑起来，白耗 push2 配额。
- **不设 15:00 下限**（与 `set_review_schedule` 的刻意差异）：港股有半日市（12:00 收盘），用户也可能只想盘中拉一次快照。已有测试 `test_hk_schedule_has_no_time_floor_unlike_review` 同时断言「港股 09:30 被保留」与「复盘 09:30 被抬到 15:00」，防止后人照抄下限逻辑。
- 时区挂 `Asia/Hong_Kong` 而非 `Asia/Shanghai`：两者同为 UTC+8 且均无夏令时，偏移一致，但语义上港股 job 该挂港股时区。

#### 🔴 实施中发现的服务层缺陷（已在 job 层挡住，服务层待决）

`sync_hk_adj_factor` 是**逐批 merge** 落盘的：即使有标的拉取失败，成功那部分也已写进 `adj_factor_hk`。`failed` 只用于决定「要不要落空库当凭据」，**并不回滚已 merge 的行**。于是因子库**非空但不完整**，`load_hk_adj_factors` 的三态闸门判定为「非空」而放行，失败标的在 `_apply_adj_factor` 里拿到 `cum_factor.fill_null(1.0)`，被当成「该股从无分红」—— **前复权静默偏高、指标静默失真**。

这正是 CONTRIBUTING §4「fail closed，绝不静默切换到错误数据或错误语义」要拦的情形。PR2e 的处理：

- **job 层已挡**：`run_hk_sync` 在因子阶段报失败时跳过 enriched 并记 error，宁可本轮不出数，等下一轮补齐。
- ⚠️ **服务层未改**：手动 API 路径（`POST /api/hk/sync_enriched`）仍会踩到同一个洞。修法有两种（因子库按轮次原子替换 / `load_hk_adj_factors` 增加「完整性凭据」维度），涉及 PR2d 已提交代码的口径变更，**留待单独决策**，不在本 PR 顺手改。

#### 阶段依赖

维表失败 → **软失败**，沿用上一份维表继续（`sync_instruments_hk` 拉取失败本就返回 0 且不写盘）；维表为空 → **硬中止**；日K失败 → 中止 enriched；因子失败 → 中止 enriched（见上）；末尾**无条件** `rebuild_views()` —— `sync_hk_daily` 只落 parquet 不刷视图、`sync_hk_enriched` 仅在 `written>0` 时刷，漏了就是 CONTRIBUTING 明列的拒收情形「写入后存在已知缓存不一致」。

**PR2 未做（明确留待后续，不是遗漏）**：

| 项 | 状态 | 归属 |
|---|---|---|
| **能力矩阵 market 维** | 刻意延后：港股 kline 绕过偏好路由直连，矩阵暂无消费方。**在此登记以免被误读为漏做** | PR3 |
| ~~**复权两源互验**~~ | **已取消**：腾讯 `hkfqkline` 的 qfq 实测是**等差**投影，与东财 `fqt=1` 的乘性口径不可能数值一致，该互验前提不成立（§3.6）。复权正确性改由「F10 分红额 ↔ 加性偏移逐位吻合」锚定，已 hermetic 化，不再需要真实网络 | — |
| ~~**调度接线**~~ | **已完成（PR2e）**：独立 job `hk_daily_sync`，默认关闭，与 A 股盘后管道**单向让路**。实现与隔离理由见下方「PR2e 完成情况」 | — |
| **成交量单位余量** | `pipeline.py:813,2422` 已修；`strategy/scoring.py:179`、`backtest/engine.py:1979`、`strategy/intraday_features.py:106`、`api/monitor_rules.py:558,675`、`strategy/monitor.py:1766,1796` **暂缓**，明细见 §3.1.2。港股进打分/回测链路的那个 PR 必须一并改，否则 VWAP 低 100 倍会让港股永远打不出分 | PR4+ |
| **待复测（需网络）** | 港股 `pz` 上限（疑为 100）；全列表代码前缀分布（纯净性护栏用）；**`t:3=主板 / t:4=GEM / t:1=REITs / t:2=债券` 的语义**（`FS_HK_EQUITY` 目前只靠单次早期探针支撑）；东财港股 kline 的 **amount 单位**（元 vs 万元，腾讯侧疑似万元） | PR3 前 |

> 🔴 **复测当前被硬阻塞（2026-11 实测）**：`push2.eastmoney.com` 及其全部镜像（`82.push2` / `17.push2` / `push2delay` / `push2his`）对本机 IP 施加**应用层封禁** —— DNS 解析正常（`14.103.191.91`）、TCP 可连、**TLS 1.3 握手成功且证书有效**，随后服务端在**不返回任何 HTTP 响应**的情况下直接断开（`curl: (52) Empty reply`，`http=000`，耗时 0.12–0.19s）。加浏览器 UA + Referer 无变化。同期 `datacenter-web.eastmoney.com`（F10 报表）、`www.eastmoney.com`、腾讯 `web.ifzq.gtimg.cn`、新浪 `hq.sinajs.cn` **全部 200 正常**，故非本机网络/DNS/TLS 问题，而是 push2 集群的 IP 级反爬封禁（本会话早先 push2 尚可用，是密集探测触发）。
>
> **影响面**：一切经 push2 的读取（A 股与港股的列表 / 实时快照 / 日K）当前均不可用，只有 `datacenter-web` 的 F10 报表（分红、财务）仍可访问。上表前三项**都是 push2 独有问题**，其他数据源无法替代验证 —— `t:N` 的语义只有东财自己知道。
>
> **风险是有界的**：`FS_HK_EQUITY` 已取保守口径（只要 `t:3` 主板 + `t:4` GEM，刻意排除 `t:1` REITs / `t:2` 债券，避免污染股票宇宙的各类分母），且第二道防线 `partition_hk_derivatives` + `_HK_EQUITY_CODE_PREFIXES = ("0", "8")` 仍在。`hk.py:73-76` 的 ⚠️「改动前必须重新实测」注释保持有效。探针脚本 `backend/_scratch/probe_hk_fs.py` 已写好（gitignored），封禁解除后可直接复跑。**在复测完成前不要修改 `FS_HK_*` 任一常量。**

### PR3 完成情况（只读看盘 + 指数）—— 后端指数栈

**状态：后端指数通路已完成并真机验证；前端未开始。** 本节只登记已完成的后端部分。

#### 落地文件

| 文件 | 变更 |
|---|---|
| `backend/app/services/index_const.py` | 改为 **per-market**：`core_index_names(market)` / `core_index_symbols(market)`；未登记市场抛 `ValueError`，**不回退 CN** |
| `backend/app/plugins/sina/client.py` | `_get_text(base, path, params=None, *, encoding=None, referer=None)` + `HQ_BASE`（原实现写死 A 股端点与 UTF-8） |
| `backend/app/plugins/sina/hk_index.py` | **新增**。27 只显式白名单 + 19 字段快照解析 + 斜杠/横杠日期容错 + `map_hk_index_quote` / `fetch_hk_index_spot` |
| `backend/app/plugins/tencent/client.py` | `KLINE_DAILY_MAX = 800` + `kline_daily(code, count)`（复用既有 `IFZQ_BASE`，实测与 `web.ifzq.gtimg.cn` 字节一致） |
| `backend/app/services/index_sync_hk.py` | **新增**。`sync_hk_index_daily` / `iter_hk_index_daily_frames` / `fetch_hk_index_quotes` / `hk_index_universe`，**三态分类**（ok / no_coverage / error） |
| `backend/app/tickflow/repository.py` | mkdir 元组 + `_register_views` + `rebuild_views` 三处同步加 2 个指数目录；新增 `append_hk_index_enriched` / `refresh_hk_index_views` |
| `backend/app/api/hk.py` | 新增 `GET /api/hk/indices`、`GET /api/hk/index_daily`、`POST /api/hk/sync_index_daily` + 2 个私有辅助 |
| `backend/tests/test_hk_index.py` | **新增 54 例**（白名单契约 / 快照字段口径 / 腾讯列序 / 同步三态与市场隔离 / API 契约 / 降级路径），全离线 |
| `backend/tests/test_hk_storage_layout.py` | `HK_DIRS` 3 → 5（加两个指数目录） |
| `CONTRIBUTING.md` | §4 指数契约**中英两处**同步为 per-market，并新增港股指数端点条目 |

真机验证（2026-11）：`sync_hk_index_daily` 写入 **1680 行 / 14 只**，区间 2020-10-27 → 2026-11-13，`failed=[]`、`no_coverage=13`；HSI 2026-11-13 OHLC 与新浪**逐位吻合**；enriched 全表 `close == raw_close`（指数无复权因子，符合预期）；A 股 `kline_index_daily` 保持空（闸3 未破）。API 8 项手工检查全绿，其中**降级路径的 `change_pct`（-0.01545）与实时路径完全一致** —— 两条独立链路（新浪字段 8 vs 腾讯相邻收盘价）互验，证明「÷100 口径」与「列序」都对。

#### 与原方案的偏差（均为实测后修正）

| 方案原文 | 实测结论 | 处理 |
|---|---|---|
| 「`index_hk_spot_sina` 实测返回 **43** 个指数」 | 实测宇宙为 **42**：真港股 27 + 纯 A 股污染 12 + **前缀像港股但实为跨市场 3**（`HSCASI` 恒生**A股**软件开发 / `CESA80` 中华**A80** / `CES300` 中华**沪深港**300） | 白名单取 **27**；🔴 那 3 只证明**前缀规则会说谎**，故用显式白名单，绝不用前缀判定或黑名单 |
| 指数复用 `kline_sync_hk.py` | 该模块文档字符串明写「行情/K线直连东财」，复用清单全是东财专属；港股**指数**走新浪+腾讯 | 按仓库既有配对惯例（`kline_sync`/`kline_sync_hk`、`instrument_sync`/`instrument_sync_hk`）**新建** `index_sync_hk.py`，不污染东财模块 |
| 在 `/api/index` 上加 `market` 参数 | `hk.py` 模块文档字符串已明写既有教义：「刻意独立成 `/api/hk/*` …… 分叉会漏 —— 目录级/路由级隔离比 `if market == HK` 可靠」；且 `/api/intraday/indices` 绑全局 QuoteService 缓存、兜底读 A 股目录 | 新增独立路由；**响应形状与 A 股一致**（`rows`/`count`/`source`），前端换端点即可复用渲染 |

#### 为什么指数不用东财（三源选型）

| 源 | 结论 |
|---|---|
| 东财 push2 | 🔴 **应用层 IP 封禁**（详见上方 PR2 遗留项），且港股指数需 `py_mini_racer` 执行 JS 解密不可逆参数 —— 该包在 **aarch64 上无 wheel**，属已知陷阱 |
| 新浪 `hq.sinajs.cn` | ✅ 快照可用，**27 只全覆盖**，但只有实时值、无历史日K |
| 腾讯 `ifzq.gtimg.cn` | ✅ 日K可用，OHLC 与新浪**逐位吻合**，但**只覆盖 14 只** |

⇒ 快照走新浪、日K走腾讯。🔴 **腾讯日K列序为 `[date, open, CLOSE, high, low, volume]`，收盘在 index 2**（不是 index 4），且所有值以**字符串**下发。

#### 🔴 成交量单位：存原值，绝不换算

HSI 2026-11-13 实测：新浪 成交量 `195857007` / 成交额 `11525719783`；腾讯日K 第 6 列 `195857007140` = 新浪成交量 × **恰好 1000**，且 ≠ 新浪成交额。新浪自身的 成交额/成交量 = **58.84**（一个合理的港股均价），而腾讯那个数按成交额读（1958 亿港币）对恒指全成份也合理。**两种读法都站得住 ⇒ 任何换算都是编造。**

处理：日K存储**根本没有 `amount` 列**，`GET /api/hk/index_daily` 返回 **`"volume_unit": None`**。指数从不做量的定量运算（无流通股本 → 无换手率；MA/MACD 只用 close）。

#### 🔴 三态分类（不是两态）

腾讯无覆盖的 13 只若记为「失败」，调度器会**每轮都告警**并把部分失败误判 —— 正是 PR2 `sync_hk_adj_factor` 那个缺陷的翻版（「部分失败但存储非空 → 下游三态闸门误判完整」）。故 `iter_hk_index_daily_frames` 对**每只**都 yield `(symbol, frame, status)`，`failed` 只装真错误，无覆盖记 INFO 并注明**实时快照对 27 只全部可用**。

无日K的 13 只：`CSHK100 CSHKDIV CSHKLC CSHKLRE CSHKMCS CSHKME CSHKPE CSHKSE CSRHK50 HKL HSIDI HSSCID HSSCPB`。
有日K的 14 只：`HSI HSCEI HSTECH HSCCI VHSI HSMBI HSMOGI HSMPI CES100 CES120 CES280 CESG10 CESHKM GEM`。
⚠️ 另实测 `HKL` 快照返回**过期日期 2026/10/30**（两周前）—— 源确实会下发陈旧行，前端不得假定快照日期就是今天。

#### 🔴 `filter_halt_days` 的 null 成交量陷阱

`app/indicators/pipeline.py:1074-1088` 用 `(volume == 0) & (amount == 0)` 判停牌；polars 里 `null == 0` → `null`，`~null` → `null` → **该行被过滤掉**。实测 58 行 null 成交量 ⇒ `compute_enriched` 返回 **0 行（静默全量丢数）**；把 volume 填 `1.0` 后 58→58、enriched `(58, 64)`、`ma20` 正常。**完全省略 `amount` 列是安全的**（第二个谓词被跳过），**null 成交量不安全**。

#### PR3 剩余（未开始）

| 项 | 状态 |
|---|---|
| 前端共享层 + `marketContent.ts` | ❌ 仓库中**不存在** `Market` 类型、`market=HK` 参数或市场切换器；⚠️ `Market*` 命名空间已被 `MarketClock`/`MarketPhase`/`MarketSnapshotRow`/`marketTradingStatus` 占用，新名须避撞 |
| K线 / 自选 / 详情 | ❌ `lib/kline.ts:16-30` 是唯一权威 options 工厂且只指向 `/api/kline/daily`；🔴 `lib/stockPreviewStore.ts:17-18` 的正则 `/^\d{6}\.(SH\|SZ\|BJ)$/` **直接拒掉 5 位 `.HK`**，详情抽屉打不开（第一道硬墙） |
| 15 分钟延时标注 | 🟡 后端已在两个新路由暴露 `quote_delay_minutes`；前端**零锚点**（全仓无「延时」二字），且 `QuoteFreshness.state='delayed'` 语义是**异常**（文案「请检查数据源与连接」）→ **不可复用**，需新建独立徽章 |
| 空态契约文案 | ❌ 组件齐备（`EmptyState`/`ErrorState`/`MissingCapChip`），但「港股无概念板块数据源」文案不存在 |
| 能力矩阵 market 维（`:918`） | ❌ PR2 刻意延后至此，仍未做 |
| 指数同步接调度 | ❌ 未接线（当前只有手动 `POST /api/hk/sync_index_daily`） |
| 前端三份硬编码指数副本 | ❌ `Layout.tsx:71-77` / `Indices.tsx:58-63` / `review/Page.tsx:787-801`；且 `review/Page.tsx:793` 的缩写表把**恒生科技指数**和**恒生中国企业指数**都压成「恒生」 |

## 15. 风险

| 风险 | 后果 | 对策 |
|---|---|---|
| **复权口径** | 缺因子时静默用 raw 价算收益 = CONTRIBUTING §3 警告的「看起来对但错」 | **已接线**（§3.6）：东财 F10 分红事件自建乘性因子，落 `adj_factor_hk/`，`sync_hk_enriched` 默认 `require_adj_factor=True`。🔴 原先记的「腾讯 `hkfqkline` 直出前复权价，不需自己拿因子重算」**是错的** —— 它是**等差**投影，只能整条替换价格序列，无法与本地 `fqt=0` 原始价混用，也反推不出因子（汇丰身上按比值差法产出 207 个假事件）。缺复权数据时仍 **fail closed** 不出数，绝不静默用 raw 价 |
| **港股双柜台** | 同一公司有港币(`09988`)与人民币(`89988`)两个代码，实测 `m:128 t:3` 里两者**并存** → 成交额重复计算、量比分母错、行业排名同一家公司占两席 | **策略已定（§3.1.1）**：都入库 + `is_cny_counter` 标记，市场级统计只计港币柜台。PR2 落地时用 `tests/test_hk_dual_counter.py` 钉住；**该策略影响 PR6 行业排名正确性**，不能拖到 PR6 才发现漏了 |
| **东财市场码用错** | `m:116` 返回 **17580** 条含窝轮/牛熊证（实测样本 `14192 中寿信证七一购A`），当成股票列表会污染全市场统计、涨跌幅榜、量比 | 列表固定用 `fs=m:128 t:3`（主板 2621）或 `m:128 t:3,t:4,t:1,t:2`（全市场 4748）；**分隔符是空格不是 `+`**；写测试断言列表里不含衍生品代码段 |
| **交易日历** | 最阴险的不是崩，而是**把节假日当交易日**（港股复活节/圣诞/农历与 A 股完全不同） | oracle + provenance + **半日市表**；实盘/告警路径拒绝 weekday_fallback |
| **报告货币** | 股价港元 × 财报人民币 → PE/PB/市值错 | `CURRENCY`/`IS_CNY_CODE` 显式判定 + 折算或标注 |
| **东财单源（财务/分钟/行业/每手股数）** | 这几类**只有东财一家**，断供即无数据 | 直连走 `eastmoney/client.py` 既有每主机令牌桶+熔断+封禁池（**我们自己退避重试**，不像 akshare `#7465` 起限流直接失败）；再加 last-good 快照 + 新鲜度标记 + 显式降级 |
| **直连爬虫接口的固有风险** | 东财/新浪/腾讯的接口**无契约保证**，改字段、改市场码、加加密都会静默断 | 双源互备（日K/实时/指数/AH 均有两源）+ 插件隔离测试 + 能力矩阵 `usable` 门控 + **每个接口落地时活体验证并记录真实端点**（照抄 akshare 函数名当结论是本次已犯过的错）。⚠️ 该风险**已实证发生**：2026-11 `push2.eastmoney.com` 全集群对本机 IP 应用层封禁（TLS 握手成功但不返回 HTTP 响应），同期 `datacenter-web` / 腾讯 / 新浪均正常 —— 说明封禁是**按集群**而非按域名整体，多源互备确实救回了部分能力。实测细节见 §14「待复测」 |
| **（条件性）akshare 版本下限** | *仅在 §3.5 决定启用该 extra 时适用*：解析到 ≤1.20.24 会拉进 Linux 专用 `py-mini-racer` 0.6.0，该包**无 aarch64 wheel** → ARM64 Linux 源码编译 V8，部署炸 | **pyproject 钉 `akshare>=1.20.25`**（该版起统一为无标记的 `mini-racer>=0.13.0`，双架构 + musl 均有 wheel）。详见 §3.5 |
| **（条件性）Docker 默认不装 extra** | *同上，仅在启用 extra 时适用*：`BACKEND_EXTRAS` 默认空 → 财务类在容器里不可用，用户只看到置灰卡片 | 需明确决策：保持可选（UI 带安装按钮）还是进默认 extras（镜像 +21 MB） |
| **串市场** | 港股页面显示 A 股数据（QuantMind 出过此事故） | 五道闸 + 串市场回归测试 + **AST 架构守卫**（`tests/test_market_architecture_guard.py`，PR1 已落地；原计划的 CI grep 会命中我们自己引用反模式的 docstring，已弃用） |
| **半日市** | 半日市下午误报「盘中」，实时刷新/提醒逻辑错乱 | `sessions` 按日期变长 + 半日市日期表（**QuantMind 也没做，TSP 要做对**） |
| **行业分类混排** | 港股行业与 A 股行业混在一个维度排名 → 无意义结果 | 行业排名按 market 分开计算 + 测试断言 |

## 16. 明确不做 / 后续再议

**本期明确不做**

- 美股（地基验证过后再议；差异更大：夏令时 / lot size / 财报口径 / 盘前盘后）
- 港股概念板块、五档盘口（**无免费数据源**）
- 港股回测、因子挖掘、策略生成、模拟盘、实盘下单
- 港股连板 / 涨停 / 情绪周期 / 龙虎榜（A 股独有语义）
- 跨市场汇率换算、多货币合并账务、跨市场资产/收益曲线合并
- A 股与港股的合并全市场排名 / 合并选股池

**后续再议**

- **CCASS 聪明钱**：港股独有的「主力资金」数据（中央结算持股 top50 席位变动）。QuantMind 为它写了 `hk_smart_money.py` + 11 个前端组件（`HkSmSeatBoard`/`HkSmConsensusBoard`/`HkSmEvidenceCard`/`HkSmTypeBoard`/`HkSmAdvantageFace`/`HkSmPersistenceFace`/`HkSmEcoFace`/`HkSmOverviewBand`/`HkSmSeatDrawer`/`HkSmStockDrawer`/`HkSmartMoneyPanel`）+ 6 个测试文件，是港股分析的重头戏，但成本高。akshare 无 CCASS 接口，需另找源
- **港股指数成份权重**：中证港股通系列（`index_stock_cons_weight_csindex`）
- **恒生行业指数行情**：`HSMBI`/`HSMPI`/`HSMOGI` 等只有零星几个，覆盖不全，只能作行业板块的补充
- **全球指数**：`index_global_spot_em`/`index_global_hist_em`/`index_global_hist_sina`

## 17. 附录：QuantMind 调研结论

QuantMind（同目录项目，AGPL v3，~2694 文件 / 877k 行）是 A/港/美/期货/币五市场平台，港股相关**113 个文件**。

### 17.1 TSP 能超过 QuantMind 的四处

| # | 项 | QuantMind 现状 | TSP 方案 |
|---|---|---|---|
| 1 | **lot size** | `market_rules.py:5-7` 头注释明确承认：「HK 按每手股数（board lot，缺省 1 表示按标的元数据，**未接入时退化为 1 股**）」；`lot_size_for_symbol():458-467` 非 CN 直接返回 `HK_RULES.lot_size = 1`；护栏里写「平台按 1 股简化，不要写 100 股」 | `stock_hk_security_profile_em.每手股数` → `instruments_hk.lot_size`，**用真值** |
| 2 | **半日市** | `market_sessions.py:32-35` HK sessions 固定 `AM 09:30-12:00` / `PM 13:00-16:00`，**未处理半日市** | `sessions` 按日期变长 + 半日市日期表 |
| 3 | **共用页面** | market-analysis / stock-terminal **fork 页面**，出过两起事故（§8） | 全线共用页面 + 适配器 |
| 4 | **港股多空辩论** | `trading-agents/` **零 market 引用**，完全不支持港股 | 共用辩论页面 + 市场护栏注入 |

### 17.2 可直接抄的纪律

- **prompt 市场护栏**：`hk_market_guardrail.md`，仅非 A 股市场注入（§9.1 已全文摘录要点）
- **fail-closed 文档串**（`hk_smart_money.py` 头注释，字字可抄）：
  - 无产物（`latest.json` 缺失）→ `HkSmartMoneyNotBuilt`，router 映射 **404 `hk_smart_money_not_built`，「页面空态不白屏」**
  - 「**零入柱（evidence_only）是真裁定不是故障**：共识榜空态、类型面板照常（标 `is_admitted=false` 与最强变体对照）——**本层绝不把 evidence_only 当错误**」
  - 「**缺失一律 None（前端「—」），绝不补 0**」
  - 「胜率 = `fwd_exc_10 > 0` 占比（**事后统计，非预测**）」
  - 「统计口径与 validate **一字不差**」
- **视图前缀隔离**：`quanthk_hub.py:45-92` 子类只换数据根 + 视图前缀 `qhk_*`
- **缓存 key 带 market**：`research.py` `_HK_STOCK_NAMES_CACHE.get(market)`
- **空态即契约**：`marketContent.ts:100-112`（§8 已摘录）
- **同值不同源立具名常量 + 平价测试钉住**：`market_rules.py:196-209` CN 撮合佣金万3 vs 券商实收万2.5 → `CN_BROKER_COMMISSION_RATE` + `test_rule_parity` 钉住三处
- **降级必须可拒绝**：`qlib_data_builder.py:481-490` 审计失败 → **拒绝写入而不是写坏因子**
- **数据源选型用抽样对比决策**：`compare_akshare_yahoo.py` 统计一致率与覆盖情况后才决定「以 akshare 为准」
- **路径解析收敛到一个模块**：`quantdb_paths.py:34-75` env override → 容器挂载 → 本地盘 → 仓库根，**「目录非空才算可用」**
- **日历别名表**：`trading_calendar.py:143-162` `_MARKET_ALIASES`（前端传惯用名 HKEX，`exchange_calendars` 用 XHKG）；`:164-166` 注释「少了这层别名，`get_market('NYSE')` 会抛错」
- **日历覆盖健康检查**：`xcal_coverage()`（`:297-336`），容器内实测 XHKG 覆盖到 **2027-09-24**

### 17.3 必须避开的坑

| 坑 | 证据 | 后果 |
|---|---|---|
| **fork 页面** | `docs/推理中心三市场_设计方案.md:9-14` | 1013/1010 行互抄、diff 291 行双向漂移、**切到美股渲染 A 股页面** |
| **只改标题不改口径** | `docs/首页六宫格多市场_设计方案.md:11-14,36-58` | 「**标题改了、口径没改，比不切换更误导**」 |
| **未知市场兜底 CN（过滤场景）** | `market_rules.py:395-413` 注释 | 「否则『不传 market』会**静默变成『只看 A 股』**」 |
| **两种 symbol 口径并存** | `ccass_top50` 用 5 位 stock_code、`hsgt_south` 用 4 位+.HK | 跨数据集 join 错位 |
| **字符串比较枚举** | `market_rules.py:96-105` `side_text()` 曾写 `str(OrderSide.SELL) == "OrderSide.SELL"` | 永远不成立 → **印花税静默归零** |
| **两套日历实现并存** | `shared/trading_calendar.py`（async + exchange_calendars + PG override）vs `services/engine/data_platform/calendars/calendar.py`（sync ABC + DB loader + 周末降级） | TSP 只能有一套 |
| **两套 Adapter 抽象并存** | `data_platform/base.py::OfflineDataSourceAdapter` vs `rd_agent/market_adapters/base.py::MarketAdapter`；统一的 `MarketAdapter` SPI（`多市场可插拔架构_设计方案.md:29-45`）**只在文档里，代码没实现** | 抽象漂移 |
| **DatasetSpec 重复声明** | `global_market_console.py:38-47` 与 `quantdb_datasets.py:1-31` 重复 | 声明不一致 |
| **路径硬编码** | `quantdb_paths.py:3-6` 注释 | 便携安装时「**读空 → 静默降级**」 |
| **141 只子集当全市场** | `quanthk_feed.py:7` 注释「而非 sector/141 只子集」 | 行业统计口径错，后期返工 |
| **无跨市场汇率层** | `market_rules.py:11-12` `currency` 仅展示用，FX 只出现在 AH 溢价与基差数据集 | 跨市场合并计算无从下手（TSP 本期不做合并，暂无影响） |

### 17.4 QuantMind 港股数据集清单（参考）

`global_market_console.py:453-596` 的 HK 分支声明：

| 数据集 | 内容 | layout |
|---|---|---|
| `akshare_valuation` | akshare 真实估值：PE/PB/PS/PCF + 排名 | symbol |
| `akshare_financial` | akshare 财务指标：EPS/ROE/市值/股息率 **21 项** | symbol |
| `akshare_profile` | akshare 公司资料：**行业**/董事长/员工数等 | symbol |
| `ccass_top50` | 港股 CCASS top50 机构持股，**stock_code 5 位** | partition |
| `hsgt_south` | 港股通南向资金持仓，**symbol 4 位+.HK** | partition |
| `ah_premium` | A/H 配对溢价率日截面（A收盘=本地QuantDB，汇率=中行折算价） | partition |
| `ah_membership` | AH 配对清单（`stock_zh_ah_name`） | single |
| `hsgt_membership` | 港股通成分（`stock_hk_ggt_components_em`） | single |
| `index_weights` | 中证港股通系列指数成分权重（csindex） | symbol |
| `adjust_factors` | **由付费源昨收推算的复权因子链**（`daily_backward` 用） | symbol |
| `l1_factors` | 本地计算的量价因子日频分区 | partition |

数据湖目录：`_MARKET_DIR_NAMES`（`training_runtime.py:33-39`）CN→`quantdb`、**HK→`quanthk`**、US→`quantus`、CRYPTO→`quantbc`、FUTURES→`quantfutures`。

港股市场规则：`HK_RULES = MarketTradingRules(market=Market.HK, currency="HKD", t_plus_1=False, lot_size=1, commission_rate=0.0003, commission_min=3.0, stamp_duty_rate=0.001, has_price_limit=False)`（**无 `transfer_fee_rate`**，A 股有）。`has_price_limit=False` 的下游消费：`local_market_data.py:502-508` → `limit_up=inf, limit_down=0.0`。

港股专属 API：`services/api/market_analysis_hk/`（`hk_smart_money.py` / `hk_opportunity.py` / `hk_heatmap_drill.py` / `quanthk_feed.py` / `router.py`）+ `routers/admin/quanthk_console.py`。

港股专属前端组件（`electron/src/features/market-analysis-hk/components/`）：`HkAhPanel` · `HkSectorValuationPanel` · `HkDividendCalendarPanel` · `HkSouthPanel` · `HkValuationPanel` · `HkHeatmapDrillCard` · `HkIndexCards` · `HkOpportunityPanel` · `HkCcassPanel` · `HkBreadthCard` · `smart-money/*`（11 个）+ `hkSmartMoneyVocabulary.ts`；`stock-terminal-hk/components/HkDetailBody.tsx`。
