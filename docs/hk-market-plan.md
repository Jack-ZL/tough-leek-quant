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
| 港股日K（腾讯，**含前复权**） | `web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?param=hk00700,day,,,5,qfq` | code=0，5 根，末根同上 + 复权因子字段 |

⚠️ **三个必须写进 PR2 的坑**：

1. **市场码是 `m:128` 不是 `m:116`。** `m:116` 返回 **17580** 条，混入大量窝轮/牛熊证（实测样本：`14192 中寿信证七一购A`、`69128 华虹汇丰八甲熊J`）。`m:128` 才是股票。子类实测：`t:1`=493(REITs) `t:2`=1324(债券) `t:3`=2621(主板) `t:4`=310(GEM) `t:5`=5903(窝轮牛熊证)。**分隔符是空格不是 `+`**（`fs=m:128 t:3`）。
2. **单标的 secid 前缀 `116.` 与 `128.` 都能用**，实测返回完全相同的数据。但**列表用 128、单标的前缀需统一选定一个**并写测试钉住，否则两套前缀混用会在缓存键与去重上出问题。
3. **港股有双柜台（港币/人民币），同一公司两个代码。** `m:128 t:3` 里 `89988 阿里巴巴-WR`、`89888 百度集团-WR`、`89618 京东集团-SWR` 是人民币柜台，与 `09988`/`09888`/`09618` 港币柜台是**同一家公司**。

   **已定策略（2026-11-15）**：两个代码都入库，`instruments_hk` 加 `is_cny_counter: bool` 标记；**所有市场级统计（成交额合计、涨跌家数、量比分母、行业排名）只计港币柜台**。

   为什么保留而不是直接不入库：人民币柜台是真实可交易标的，用户搜 `89988` 时应能命中并看到行情，入库时过滤会让搜索与个股页 404。为什么用标记而不是入库时合并：把「是否参与统计」的决定权留在查询层，日后想改口径（例如某些统计需含双柜台）不必重跑全量入库。**该策略影响 PR6 行业排名正确性，PR2 落地时必须有测试钉住。**

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

**单源无兜底**：财务三表、财务指标、分钟K、每手股数、所属行业 —— **全是东财**。

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

## 4. 存储与隔离：五道闸

| # | 闸 | 说明 |
|---|---|---|
| 1 | **独立港股模块** | 直连方案下不再是「独立插件目录」，而是**现有插件内的独立港股模块**：`plugins/eastmoney/hk.py`、`plugins/tencent/hk.py`、`plugins/sina/hk.py`，**只服务 `.HK`，A 股 symbol 返回空**；共用同插件的 `client.py`（复用限速/熔断/封禁池）。隔离性靠 PR1 的 `assert_symbol_market` 护栏 + 模块级 market 门控保证，不靠目录边界 |
| 2 | **`availability()` 懒导入** | 港股模块导入失败或依赖缺失只置灰卡片，不破坏启动（CONTRIBUTING §4 要求插件加载失败需隔离测试）。**若最终启用 akshare extra，该 extra 缺失同样只置灰财务类卡片** |
| 3 | **独立数据目录** | `instruments_hk` / `kline_hk_daily` / `kline_hk_enriched` / `kline_hk_minute` / `kline_hk_index_daily`。现有 **11 处 `kline_daily/**/*.parquet` 递归 glob**（`repository.py:175,177,2808,2809`；`api/kline.py:2240`；`jobs/daily_pipeline.py:1060`）**扫不到港股** |
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
| **复权正确性** | 断言腾讯 `hkfqkline` 的 `qfq` 序列与东财 `fqt=1` 序列在重叠区间**数值一致**（两源互验）；取不到复权数据时 **fail closed**，绝不静默回落 raw 价 |
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

## 15. 风险

| 风险 | 后果 | 对策 |
|---|---|---|
| **复权口径** | 缺因子时静默用 raw 价算收益 = CONTRIBUTING §3 警告的「看起来对但错」 | **风险已降低**：腾讯 `hkfqkline/get?param=hk00700,day,,,5,qfq` 实测**直接返回前复权价**（2026-11-15），不需自己拿因子重算。东财 `kline/get?fqt=1` 亦为前复权。仍保留：缺复权数据时 **fail closed** 不出数，绝不静默用 raw 价 |
| **港股双柜台** | 同一公司有港币(`09988`)与人民币(`89988`)两个代码，实测 `m:128 t:3` 里两者**并存** → 成交额重复计算、量比分母错、行业排名同一家公司占两席 | **策略已定（§3.1.1）**：都入库 + `is_cny_counter` 标记，市场级统计只计港币柜台。PR2 落地时用 `tests/test_hk_dual_counter.py` 钉住；**该策略影响 PR6 行业排名正确性**，不能拖到 PR6 才发现漏了 |
| **东财市场码用错** | `m:116` 返回 **17580** 条含窝轮/牛熊证（实测样本 `14192 中寿信证七一购A`），当成股票列表会污染全市场统计、涨跌幅榜、量比 | 列表固定用 `fs=m:128 t:3`（主板 2621）或 `m:128 t:3,t:4,t:1,t:2`（全市场 4748）；**分隔符是空格不是 `+`**；写测试断言列表里不含衍生品代码段 |
| **交易日历** | 最阴险的不是崩，而是**把节假日当交易日**（港股复活节/圣诞/农历与 A 股完全不同） | oracle + provenance + **半日市表**；实盘/告警路径拒绝 weekday_fallback |
| **报告货币** | 股价港元 × 财报人民币 → PE/PB/市值错 | `CURRENCY`/`IS_CNY_CODE` 显式判定 + 折算或标注 |
| **东财单源（财务/分钟/行业/每手股数）** | 这几类**只有东财一家**，断供即无数据 | 直连走 `eastmoney/client.py` 既有每主机令牌桶+熔断+封禁池（**我们自己退避重试**，不像 akshare `#7465` 起限流直接失败）；再加 last-good 快照 + 新鲜度标记 + 显式降级 |
| **直连爬虫接口的固有风险** | 东财/新浪/腾讯的接口**无契约保证**，改字段、改市场码、加加密都会静默断 | 双源互备（日K/实时/指数/AH 均有两源）+ 插件隔离测试 + 能力矩阵 `usable` 门控 + **每个接口落地时活体验证并记录真实端点**（照抄 akshare 函数名当结论是本次已犯过的错） |
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
