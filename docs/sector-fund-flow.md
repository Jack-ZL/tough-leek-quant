# 板块资金流向（sector-fund-flow）设计文档

> 状态：已实现（东财多节点主源 + 新浪二级降级 + 历史缓存回退）；同花顺留作后续扩展。
> 关联文档：`plugin-development.md`（数据源插件契约）、`CONTRIBUTING.md`（数据契约红线）。

## 1. 目标与范围

在**复盘页（Review）龙虎榜卡片下方**新增「板块资金流向」模块，展示当日盘后
资金净流入/净流出居前的**板块（行业 + 概念两个维度）**，回答「今天钱流向哪里」。

- 只展示**盘后快照**（当日最新排名），不做盘中实时刷新。
- 只做**板块/概念级**，不做个股级。
- 数据获取**一天一次**（盘后），失败就展示「暂无数据」，不做重试风暴、不做开关。

## 2. 数据源分级（三级降级）

板块/概念级资金流是相对小众的公开数据集，免费且**相互独立**的来源有限。
经实测（详见 §8 实测结论），当前实现为**东财多节点主源 + 新浪二级降级**，同花顺留作后续扩展：

| 级 | 数据源 | 上游 | 字段能力 | 状态 |
|---|---|---|---|---|
| 1 | 东财 clist（自研 client） | push2delay / push2 / 82.push2 / push2his | 最全：主力/超大/大/中/小单净额、占比、涨跌幅 | ✅ 已实现 |
| 2 | 新浪财经（自研 client） | `MoneyFlow.ssl_bkzj_bk` | 行业+概念净额、涨跌幅、领涨股（无分档净额） | ✅ 已实现 |
| 3 | 同花顺 10jqka（自研 client） | data.10jqka.com.cn | 净流入、涨跌幅 | ⏸ 待扩展（需 JS 引擎解 hexin-v token） |

> 已排除：akshare（其板块资金流函数底层仍是东财/同花顺，非真正独立，且引入
> pandas/lxml 重依赖）；腾讯财经（接口文档少、字段不稳）；Tushare（需 token+
> 积分，且 ind/cnt 资金流底层仍是东财/同花顺）。

**降级链当前形态**：东财多主机顺序尝试（`push2delay` → `push2` → `82.push2` →
`push2his`，任一主机成功即停）→ 东财全失败则降级新浪（行业 + 概念各一次）→
仍失败才回退上一交易日缓存（`fallback_prev`），无缓存则 `no_data`。**不设启用/禁用
开关**，不做 env 门控、不做界面开关。爬取第三方接口的合规风险由使用者自行承担。

## 3. 归一化契约（跨源统一）

三个源字段差异大，client 层必须归一成**同一套字段**再交给服务层。前端只读统一
字段，不感知来源。

### 3.1 单条板块记录（normalized item）

| 字段 | 类型 | 契约 | 说明 |
|---|---|---|---|
| `name` | str | 必填 | 板块/概念名称 |
| `code` | str | 可选 | 源自己的板块编码（各源口径不同，仅作 key 用） |
| `change_pct` | float | 小数制 | `0.0174` = +1.74%（遵守 CONTRIBUTING §3.1） |
| `main_net_inflow` | float | 元 | 主力净流入，负数 = 净流出 |
| `main_net_ratio` | float \| None | 小数制 | 主力净流入占比，源不提供则 None |
| `super_net` | float \| None | 元 | 超大单净额（东财特有，其余 None） |
| `large_net` | float \| None | 元 | 大单净额（东财特有） |
| `medium_net` | float \| None | 元 | 中单净额（东财特有） |
| `small_net` | float \| None | 元 | 小单净额（东财特有） |
| `leader_name` | str \| None | — | 领涨股（新浪特有，其余 None） |

> 缺字段置 `None`，禁止「数值小于 1 就乘 100」之类的启发式补全（CONTRIBUTING §3.1）。
> 排序语义：`main_net_inflow` 降序 = 净流入居前。

### 3.2 服务返回容器（前端契约）

```json
{
  "state": "ok | fallback_prev | no_data",
  "source": "eastmoney | sina | null",
  "requested_date": "YYYY-MM-DD | null",
  "trade_date": "YYYY-MM-DD | null",
  "industry": {
    "inflow":  [ /* 归一化 item[], 净流入 Top100（main_net_inflow 降序） */ ],
    "outflow": [ /* 归一化 item[], 净流出 Top100（main_net_inflow 升序） */ ]
  },
  "concept": {
    "inflow":  [ /* 同上 */ ],
    "outflow": [ /* 同上 */ ]
  }
}
```

> 东财 clist 单页 `pz` 上限 100（行业 496 / 概念 504），故每个维度取净流入/净流出
> 各前 100 名，而非全量板块。

## 4. 服务设计

镜像现有 `services/dragon_tiger.py` 的非路由模式（板块资金流不属于任何路由数据集，
类似龙虎榜是 fuyao 专有非路由数据）。

### 4.1 文件划分

```
backend/app/services/sector_fund_flow.py           # 编排：降级链 + state + 缓存 + build_recap_context
backend/app/services/sector_fund_flow_clients.py   # 东财(多主机) + 新浪自研 HTTP client + 归一化
```

> 不放进 `plugins/`：资金流不是路由数据集，且用户明确不要注册/开关机制；作为
> 纯 service 内自研 client 更简单（对齐 CONTRIBUTING §1.2 保持简单 / §1.3 最小范围）。

### 4.2 client 接口（东财多主机 + 新浪）

```python
class BaseClient:
    def fetch(self, kind: str) -> list[dict]:
        """kind ∈ {"industry", "concept"}；返回归一化后的 item list。
        失败抛 SectorFundFlowError（服务层捕获后降级）；反爬返回空/异常时同样抛。"""
```

- 各自带 UA / Referer 头，独立 httpx 会话，超时上限（建议 ≤ 15s/请求）。
- 每个 client 内部把源字段映射 + 单位换算成 §3.1 契约（百分数 → 小数制 /100）。

### 4.3 状态机与降级

```python
def get_sector_fund_flow(data_dir, target=None) -> dict:
    # 1) target=None → 最近本地交易日（复用 dragon_tiger._local_trading_days）
    # 2) trade_date = dragon_tiger.resolve_trade_date(data_dir, target)
    # 3) 历史日（trade_date < cn_today()）优先读缓存，命中即返回
    # 4) 东财多主机顺序尝试 fetch(industry)+fetch(concept)
    #    任一主机成功 → state=ok，source="eastmoney"，历史日落缓存
    # 4b) 东财全失败 → 降级新浪 fetch(industry)+fetch(concept)
    #    成功 → state=ok，source="sina"，历史日落缓存
    # 5) 全部失败 → 回退上一交易日缓存（state=fallback_prev，复用 _prev_trading_day）
    # 6) 无缓存 → state=no_data
```

- `source` 字段记录「本次数据由哪个源提供」，前端展示来源角标 + 便于排查。
- 复用 `dragon_tiger.resolve_trade_date` / `_local_trading_days` / `_prev_trading_day`
  （交易日集合取自本地 `data/kline_daily/date=YYYY-MM-DD/` 分区），避免重复实现。
- 缓存：`data/sector_fund_flow/date=YYYY-MM-DD.json`（`.part` + `replace` 原子写，
  同 dragon_tiger）。历史日不可变 → 落缓存；当日不缓存（以现拉为准）。

### 4.4 取数时机（无新增 cron）

采用与龙虎榜一致的**懒加载 + 按日缓存**：

- 前端打开复盘页 / AI 复盘（`build_recap_context`）触发 API → 服务现拉一次 → 落缓存。
- 当日已取过 → 直接读缓存，不重复打接口。即满足「盘后一天取一次、之后都读缓存」。
- 17:00 定时预取**不是必需**（懒加载已保证一天一次 + 失败自愈：下次访问再试）。
  若后续确需盘后预热，可在 `daily_pipeline.start_scheduler` 加一条
  `id="sector_fund_flow"` 的 `mon-fri 17:00` cron 触发一次预取，属可选优化。

## 5. API 设计

在 `backend/app/api/market_recap.py` 增加（镜像 `/dragon-tiger` 路由）：

```
GET /api/market-recap/sector-fund-flow?date=YYYY-MM-DD
```

- `date` 缺省 → 最近已发布交易日。
- 返回 §3.2 容器；`state` 由服务层决定，前端据此渲染正常/回退/暂无数据。

## 6. 前端设计

`frontend/src/pages/Review.tsx`，在 `DragonTigerCard` 下方新增 `<SectorFundFlowCard date={dtDate} />`。

- **两个 tab**：行业 / 概念。
- **展示**：净流入 Top10 + 净流出 Top10（红涨绿跌沿用现有 `pctClass`/`fmtPct`）。
- **字段**：板块名、涨跌幅、主力净流入（元 → 亿，`fmtBigNum`）。
- **来源角标**：`source` → 东财/新浪 小徽标（`d.source` 已随契约回传，可选展示）。
- **状态降级**：`fallback_prev` 显示「当日未发布，回退上一交易日」；`no_data` /
  全部为空显示「暂无数据」。
- `api.ts` 新增 `SectorFundFlowItem` / `SectorFundFlowPayload` 类型 + `api.sectorFundFlow()`。

## 7. AI 复盘集成

`services/market_recap.py` 的复盘提示词拼装处（当前 `_build_user_prompt` 已含龙虎榜
`lhb_context` 与竞价 `bench_context`）新增 `sector_flow_context` 段，注入「## 板块资金流向」
摘要（净流入/净流出居前板块），失败返回空串不影响复盘主流程（同 `build_recap_context` 模式）。

## 8. 实测结论（2026-08 实测，已回填）

> 教训：东财 push2 在本机曾返回 HTTP:000（疑似 IP 限流/封禁）。所有 endpoint 的
> 字段名、单位、分页都需真实请求实测一页再写 client，不能只信文档/网上示例。

1. **东财 clist** ✅ 已实测：
   - `push2.eastmoney.com` 在本机 IP 被封（HTTP:000），但 **`push2delay.eastmoney.com`
     可用**（延迟行情，盘后够用）。故 client 采用多主机顺序降级：
     `push2delay → push2 → 82.push2 → push2his`。
   - 行业 `fs=m:90+t:2+f:!50`、概念 `fs=m:90+t:3+f:!50`，`fid=f62`，
     `fields=f12,f14,f3,f62,f184,f66,f72,f78,f84`，`po=1` 净流入降序 / `po=0` 净流出升序。
   - 字段映射（实测确认）：`f3`=涨跌幅（百分制原值，需 /100 → 小数制）、`f12`=代码、
     `f14`=名称、`f62`=主力净流入(元)、`f184`=主力净占比(百分制原值)、
     `f66/f72/f78/f84`=超大/大/中/小单净额(元)。「主力 = 超大 + 大单」口径已核对。
   - **`pz` 上限 100**：即使请求 `pz=600` 也只返回 100 行（行业总量 496、概念 504）。
     故每个维度取净流入/净流出各前 100 名，不做全量分页。
2. **同花顺 10jqka** ⏸：数据中心资金流需 `hexin-v` cookie，由 JS 脚本动态生成，
   需引入 JS 引擎（如 py_mini_racer）才可解；为保持依赖最小暂缓，留作后续扩展。
3. **新浪财经** ✅ 已实现为二级降级：
   - 采用 `MoneyFlow.ssl_bkzj_bk`（`vip.stock.finance.sina.com.cn/quotes_service/api/
     json_v2.php/MoneyFlow.ssl_bkzj_bk?page=1&num=1000&sort=netamount&asc=0&fenlei={0|1}`），
     返回自描述 JSON 数组（`fenlei: 0=行业 48 行 / 1=概念 181 行`）。
   - 字段映射（实测确认）：`name`=名称、`category`=板块编码、`avg_changeratio`=涨跌幅
     （已小数制）、`netamount`=净流入(元, 有符号)、`ratioamount`=净占比（已小数制）、
     `ts_name`=领涨股。**无超大/大/中/小单分档**，相应字段置 `None`。
   - 新浪板块分类口径与东财不同（如行业名几乎不重合），属正常降级路径，展示各自口径即可。
4. 反爬：东财 `push2` 封禁可通过切换延迟/备用主机绕过，无需同源重试；其他源待扩展时实测。

## 9. 测试矩阵（遵守 CONTRIBUTING §9，不依赖真实网络）

新增 `backend/tests/test_sector_fund_flow.py`，用假 client（monkeypatch）覆盖：

1. **降级链/状态机**：东财成功即停；东财全主机失败 → 降级新浪成功 `source="sina"`；
   东财+新浪全失败 → 回退上一交易日缓存 `fallback_prev`；无缓存 `no_data`。
2. **缓存**：历史日命中缓存；成功落缓存（原子写）；当日不落缓存。
3. **回退**：当日未发布 → `fallback_prev`（复用上一交易日缓存）。
4. **归一化**：各 client 的百分数 → 小数制、金额单位、缺字段 None（不伪造）。
5. **build_recap_context**：有数据/空数据/全失败三种分支。
6. **API 层**：`/sector-fund-flow` 成功 / no_data / 非法 date 400。

```bash
cd backend && uv run --extra dev python -m pytest tests/test_sector_fund_flow.py -q
uv run --extra dev python -m ruff check app/services/sector_fund_flow.py \
  app/services/sector_fund_flow_clients.py app/api/market_recap.py tests/test_sector_fund_flow.py
cd ../frontend && npx tsc -b
```

## 10. 实施步骤（建议顺序）

1. `sector_fund_flow_clients.py`：东财 + 新浪 client + 归一化（先实测 endpoint，再写）。
2. `sector_fund_flow.py`：降级链 + 缓存 + `build_recap_context`。
3. `market_recap.py`：新增 `/sector-fund-flow` 路由。
4. `services/market_recap.py`：复盘提示词注入板块资金流段。
5. `api.ts` + `Review.tsx`：类型 + `SectorFundFlowCard`。
6. `tests/test_sector_fund_flow.py`：契约测试。
7. 全量验证：pytest + ruff + `npx tsc -b`。
