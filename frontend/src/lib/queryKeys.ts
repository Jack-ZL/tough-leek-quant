/**
 * 集中管理所有 React Query key。
 *
 * - 新增查询只需在此加一行，所有消费方自动引用。
 * - SSE invalidation 基于 SSE_INVALIDATE_PREFIXES 列表，新增 key 无需改 useQuoteStream。
 */
import { type Market } from '@/lib/market'

// ===== Query Key 工厂 =====

/** 日K修正后的直接消费查询; SSE 与数据页共用, 不扩散到分钟和财务数据。 */
export const DAILY_DATA_QUERY_PREFIXES = new Set([
	'screener-cached', 'screener-kline-batch', 'market-snapshot', 'overview-market',
	'index-quotes', 'index-daily', 'watchlist-enriched', 'kline-batch', 'kline', 'kline-latest', 'stock-levels',
	'regime-history', 'regime-latest', 'regime-states', 'regime-coverage', 'regime-phases', 'regime-mainline',
])

/** 分钟落盘补拉后的体检与直接消费查询, 不影响日K/财务或独立的指数分钟数据。 */
export const MINUTE_DATA_QUERY_PREFIXES = new Set([
	'minute-gaps', 'minute-batch', 'kline-minute', 'kline-minute-range',
])

/**
 * 港股日K/enriched/维表写入后需要刷新的消费方。
 *
 * 🔴 与 A 股 `DAILY_DATA_QUERY_PREFIXES` **刻意分开**: 两个市场的写入链互不影响,
 * 混在一个集合里会让 A 股同步白刷港股查询(反之亦然), 也会让「哪些 key 属于哪个
 * 市场」这条边界在代码里消失。这是 key 前缀的归属划分, 不是市场分支控制流。
 */
export const HK_DATA_QUERY_PREFIXES = new Set([
	'hk-data-status', 'hk-instruments', 'hk-daily',
	// 分红日历的股息率分母取自 `kline_hk_daily.close`、名称取自 `instruments_hk`,
	// 两者都在港股同步链里 ⇒ 同步完必须一起失效, 否则会留着基于旧收盘价的股息率。
	// AH 溢价同理: H 侧价格取自 `kline_hk_daily.close`、消歧用的上市日与 H 股名称
	// 取自 `instruments_hk`。
	// 🔴 这里是 `has()` 精确匹配首段, 不是前缀匹配, 故必须逐条列出。
	// 🔴 南向资金流(`hk-connect-flow`)**刻意不在本集合里**: 它不读任何本地数据,
	// 纯上游实时报表, 港股同步链与它无关, 加进来只会白刷一次。
	// 🔴 波动率情绪(`hk-volatility`)**必须在本集合里**: 它读的是本地
	// `kline_hk_index_daily`(VHSI + HSI 日K), 同步完不失效就会留着按旧样本
	// 标定的分位阈值 —— 而阈值一变, 档位可能整个换档, 界面上却看不出差别。
	// 🔴 人气榜(`hk-hot-rank`)**必须在本集合里**: 榜单本身来自上游, 但**名称**
	// 左连本地 `instruments_hk` 维表 —— 维表同步完不失效, 新上市标的会一直显示
	// null 名称。⚠️ 别照抄上面那条 `hk-connect-flow`(它不读任何本地数据)。
	// 判断标准是「有没有读本地数据」, 不是「是不是港股端点」。
	'hk-dividends', 'hk-ah-premium', 'hk-volatility', 'hk-hot-rank',
])

/** 扩展数据写入后的结构、序列和列表消费方。日K仅刷新携带扩展列的查询。 */
export const EXT_DATA_QUERY_PREFIXES = new Set([
	'ext-data', 'ext-data-rows', 'ext-data-schema-all', 'ext-schema-all', 'ext-series',
	'watchlist-enriched', 'screener-cached', 'dimension-members', 'dimension-intraday',
	'table-schema', 'custom-signals-options',
])

export const QK = {
	dataVersions: ['data-versions'] as const,
	extensionPackages: ['extension-packages'] as const,
	// 全局 / 共享 (Layout 预取)
	capabilities: ['capabilities'] as const,
	settings: ['settings'] as const,
	endpoints: ['endpoints'] as const,
	version: ['version'] as const,
	preferences: ['preferences'] as const,
	soundPacks: ['sound-packs'] as const,
	dataSources: ['data-sources'] as const,
	/**
	 * 能力路由矩阵。
	 *
	 * 🔴 带 market 时 key 追加市场段（方案 §7：缓存 key 必须带 market，否则 CN/HK
	 * 响应互相污染）。不传 market 时返回裸前缀 `['capability-matrix']`：既是
	 * 「CN（后端默认）」的取数 key，也是 invalidate 的前缀 key —— 一次失效
	 * 覆盖所有市场，与既有调用点行为完全一致。
	 */
	capabilityMatrix: (market?: Market) =>
		(market ? ['capability-matrix', market] : ['capability-matrix']) as readonly string[],
	quoteStatus: ['quote-status'] as const,
	quoteInterval: ['quote-interval'] as const,
	marketTradingStatus: ['market-trading-status'] as const,
	overviewMarket: (asOf?: string) => ['overview-market', asOf ?? 'latest'] as const,
	indexQuotes: ['index-quotes'] as const,
	// 港股指数走独立端点(见 lib/api.ts hkIndices)。symbols 影响结果 ⇒ 必须入 key。
	hkIndexQuotes: (symbols: string) => ['hk-index-quotes', symbols] as const,

	// Watchlist
	watchlist: ['watchlist'] as const,
	watchlistGroups: ['watchlist-groups'] as const,
	watchlistPrepare: (symbols: string[]) => ['watchlist-prepare', symbols] as const,
	watchlistQuotes: ['watchlist-quotes'] as const,
	watchlistEnriched: (ext?: string) => ['watchlist-enriched', ext ?? ''] as const,
	// 异动边缘总览 (开启监控时才查询, 参数为 min_closeness/limit)
	abnormalOverview: (minCloseness: number, limit: number) => ['abnormal-overview', minCloseness, limit] as const,
	// 盘中异动信号聚合 (异动监控「盘中」tab)
	abnormalIntraday: (limit: number) => ['abnormal-intraday', limit] as const,
	// 不用 watchlist- 前缀: 日K历史盘中几乎不变, 若被 SSE quotes_updated 高频失效
	// (expert 1s) 会导致全自选日K每秒重拉, staleTime 形同虚设。
	// 刷新点: staleTime 过期 + Watchlist 增删自选/改蜡烛天数时的手动失效;
	// 当日最后一根蜡烛由 Watchlist 用 enriched 实时 OHLC 前端修补 (零额外请求)。
	// symbols 不进 key: 千只自选时 key 内嵌全量拼串 (12-17KB), 增删 1 只即换 key
	// 整批重拉且新旧两份共存; 数据按 symbol upsert 进单 key, 仅天数变化换 key。
	watchlistKlineBatch: (days: number) => ['kline-batch', days] as const,
	// 不用 watchlist- 前缀: 避免被 SSE quotes_updated 高频失效(expert 1s/pro 2s)
	// 导致每次都拉 TickFlow 触限流。分时图用固定 refetchInterval 刷新即可。
	// 同上不嵌 symbols: 单 key + 按 symbol 增量合并, 请求集合由视口收敛
	// (见 lib/minuteBatchIncremental.ts)。
	minuteBatch: () => ['minute-batch'] as const,
	// 🔴 market 必须进 key: 自选页切市场后若仍复用同一 key, react-query 会把上一个
	// 市场的缓存结果当当前市场渲染(串市场), 且 staleTime 内连请求都不发。
	instrumentSearch: (q: string, assetTypes?: string, market?: string) =>
		['instrument-search', q, assetTypes ?? 'stock', market ?? 'CN'] as const,

	// Screener
	screener: ['screener'] as const,
	screenerStrategies: (assetType: string = 'stock', timeframe: '1d' | '1m' | 'all' = '1d') => ['screener-strategies', assetType, timeframe] as const,
	/** 策略池 v2 (后端权威: 条目/分组/档案库), 排序是本地视图偏好不进 key */
	strategyPool: ['strategy-pool'] as const,
	strategyList: (assetType: string = 'all', timeframe = '1d', includeResearch = false) =>
		['strategies', assetType, timeframe, includeResearch] as const,
	screenerCachedSummary: ['screener-cached', 'summary'] as const,
	screenerCachedResult: (strategyId: string, asOf?: string, ext?: string) => ['screener-cached', 'strategy', strategyId, asOf ?? '', ext ?? ''] as const,
	screenerCached: (asOf?: string, ext?: string) => ['screener-cached', 'all', asOf ?? '', ext ?? ''] as const,
	screenerKlineBatch: (symbols: string) => ['screener-kline-batch', symbols] as const,
	/** 扩展列迷你图: 列集 (含显示周期和天数) + 日期 + 标的集。 */
	extSeriesAll: ['ext-series'] as const,
	extSeries: (colsKey: string, date?: string | null, symbolsKey?: string) => ['ext-series', colsKey, date ?? '', symbolsKey ?? ''] as const,
	/** 全市场轻量行情快照。🔴 key 带 market: 两个市场的行集完全不同, 共用一个 key
	 *  会让切市场后短暂渲染上一个市场的缓存 (串市场)。 */
	marketSnapshot: (market: Market) => ['market-snapshot', market] as const,
	limitLadder: (asOf?: string) => ['limit-ladder', asOf] as const,

	// Backtest
	backtestStatus: ['backtest-status'] as const,
	backtestCandidates: ['backtest-candidates'] as const,
	factorColumns: ['backtest-factor-columns'] as const,
	factorLibrary: (assetType: string) => ['factors-library', assetType] as const,
	/** 通达信兼容表/模板: 无入参, 单键即可 (P1-17 第 4 批) */
	factorTdxMeta: ['factors-tdx-meta'] as const,
	/** 统一定义目录 (信号+因子, P4) — 定义选择器数据源 */
	definitions: ['definitions'] as const,
	resourceCatalog: ['resource-catalog'] as const,
	/** 用户自定义仓位策略 (设置→定义 与 回测设置共用) */
	positionPolicies: ['backtest-position-policies'] as const,
	miningRuns: ['backtest-mining-runs'] as const,
	miningAvailability: (assetType: string, profile: string, start: string, end: string) =>
		['backtest-mining-availability', assetType, profile, start, end] as const,
	miningRun: (id: string) => ['backtest-mining-run', id] as const,
	miningResult: (id: string) => ['backtest-mining-result', id] as const,
	miningConfig: ['backtest-mining-config'] as const,
	miningLock: ['backtest-mining-lock'] as const,
	guardianOverview: ['backtest-guardian-overview'] as const,
	guardianRuns: ['backtest-guardian-runs'] as const,
	evolutionRuns: ['backtest-evolution-runs'] as const,
	evolutionRun: (id: string) => ['backtest-evolution-run', id] as const,
	labReport: ['backtest-lab-report'] as const,
	researchCandidates: ['research-candidates'] as const,
	strategyLinkOptions: (assetType?: 'stock' | 'etf') => assetType
		? ['strategy-link-options', assetType] as const
		: ['strategy-link-options'] as const,
	strategyDetail: (id: string) => ['strategy-detail', id] as const,

	// Data / Pipeline
	dataStatus: ['data-status'] as const,
	// 港股数据画像。🔴 前缀 `hk-data-status` 与 A 股 `data-status` **互斥**, 不得共用:
	// 两者是不同端点、不同缓存策略(A 股有服务端缓存, 港股刻意没有), 共用前缀会让
	// 一次失效顺带打掉另一个市场的画像, 或反过来让港股同步后 A 股卡片白刷。
	hkStatus: ['hk-data-status'] as const,
	/** 港股维表分页。过滤条件影响结果 ⇒ 必须入 key。 */
	hkInstruments: (q: string, includeCny: boolean, limit: number, offset: number) =>
		['hk-instruments', q, includeCny, limit, offset] as const,
	dataIntegrity: ['data-integrity'] as const,
	/** 分钟数据缺口体检 (发现本地分区中不完整的 标的×交易日) */
	minuteGaps: ['minute-gaps'] as const,
	pipelineJobs: ['pipeline-jobs'] as const,
	pipelineJob: (id: string) => ['pipeline-job', id] as const,
	// P1-12 layer3: 备源重跑清单
	rerunManifest: ['rerun-manifest'] as const,
	// P1-13: 个股资料(解禁/质押/回购/股东户数), 按数据集+标的分键
	stockProfile: (dataset: string, symbol: string) => ['stock-profile', dataset, symbol] as const,
	extData: ['ext-data'] as const,
	extDataRows: (id: string, date?: string, limit?: number, columns?: string) => ['ext-data-rows', id, date, limit, columns] as const,
	dimensionMembers: (id: string, field: string, value: string, date?: string) => ['dimension-members', id, field, value, date] as const,
	dimensionIntraday: (id: string, field: string, value: string, date?: string) => ['dimension-intraday', id, field, value, date] as const,
	analysisMenus: ['analysis-menus'] as const,
	analysisMenu: (id: string) => ['analysis-menu', id] as const,
	extPages: ['ext-pages'] as const,
	extPagesSourceNamespaces: ['ext-pages', 'source-namespaces'] as const,

	// Kline
	kline: (symbol: string, start: string, end: string, extColumns?: string) =>
		['kline', symbol, start, end, extColumns ?? ''] as const,
	// 批量标的名称查询键: 排序后 join — 同一集合不同来源顺序 (draft/分组成员/
	// 持仓+订单合并) 得到同一键, 复用缓存而不是各拼各的重复请求。
	instrumentNames: (symbols: readonly string[]) =>
		['instrument-names', [...new Set(symbols)].sort().join(',')] as const,
	klineLatest: (symbol: string) => ['kline-latest', symbol] as const,
	stockLevels: (symbol: string, days?: number) => ['stock-levels', symbol, days ?? 120] as const,
	klineMinute: (symbol: string, date: string) =>
		['kline-minute', symbol, date] as const,
	klineMinuteRange: (symbol: string, days: number) =>
		['kline-minute-range', symbol, days] as const,
	indexDaily: (symbol: string, start: string, end: string) =>
		['index-daily', symbol, start, end] as const,
	indexMinute: (symbol: string, date: string) =>
		['index-minute', symbol, date] as const,
	// 港股指数只有日K(无分钟端点)。start/end/limit 均影响结果 ⇒ 全部入 key。
	hkIndexDaily: (symbol: string, start: string, end: string, limit: number) =>
		['hk-index-daily', symbol, start, end, limit] as const,
	// 港股**个股**日K(前复权 + 指标)。🔴 前缀 `hk-daily` 与 A 股 `kline` 互斥,
	// 不得共用: 两个市场排序/单位/指标列都不同, 共用 key 会让切市场时短暂显示
	// 上一个市场的数字(方案 §8「标题改了、口径没改, 比不切换更误导」)。
	hkDaily: (symbol: string, start: string, end: string, limit: number) =>
		['hk-daily', symbol, start, end, limit] as const,

	// 港股财务 (PR5)。🔴 前缀 `hk-financials` 与 A 股 `financials` **互斥**, 不得共用:
	// 两个市场表集(A股5张/港股4张, 港股无股本表)、列集与**币种口径**都不同, 共用
	// key 会让切市场那一帧渲染出上一个市场的数字(方案 §8「标题改了、口径没改,
	// 比不切换更误导」)。三个 key 同前缀 ⇒ 一次 `['hk-financials']` 失效全覆盖。
	hkFinancialsStatus: ['hk-financials', 'status'] as const,
	// 四张表共用**一个** key: 后端 `GET /api/hk/financials` 一次返回全部四表, 各 tab
	// 用 useQuery 的 `select` 各取一片 ⇒ react-query 自动去重成一次请求, 组件仍保持
	// 「一个 tab 一个 hook」的结构(与 A 股五端点形态对齐, 不用改渲染层)。
	hkFinancials: (symbol: string, limit: number) =>
		['hk-financials', symbol, limit] as const,
	hkFinancialStatement: (symbol: string, table: string, reportDate?: string) =>
		['hk-financials', 'statement', symbol, table, reportDate ?? ''] as const,

	// 港股分红日历 (PR7)。窗口两端都影响结果 ⇒ 必须入 key(否则改日期不重新取数)。
	// 🔴 数据**不落库**, 每次 miss 都打一次上游报表 ⇒ 消费方必须给足 staleTime。
	hkDividends: (start: string, end: string) =>
		['hk-dividends', start, end] as const,

	// 港股通(南向)资金流。窗口两端都影响结果 ⇒ 必须入 key。
	// 🔴 不落库、不读本地数据, 每次 miss 都打一次上游报表 ⇒ 消费方必须给足 staleTime。
	// 🔴 前缀 `hk-connect-flow` 刻意**不**加入 `HK_DATA_QUERY_PREFIXES`: 港股同步链
	// 与它无关, 加进去只会在每次同步后白刷一次上游请求。
	hkConnectFlow: (start: string, end: string) =>
		['hk-connect-flow', start, end] as const,

	// AH 溢价。后端一次算全市场, 无查询参数 ⇒ 静态 key。
	// 🔴 前缀 `hk-ah-premium` **在** `HK_DATA_QUERY_PREFIXES` 里: H 侧价格取自
	// `kline_hk_daily.close`、消歧用的上市日与名称取自 `instruments_hk`, 港股同步完
	// 必须失效, 否则会留着基于旧收盘价的溢价。
	hkAhPremium: ['hk-ah-premium'] as const,

	// 港股波动率情绪 (VHSI)。`seriesDays` 影响 `series` 长度 ⇒ 必须入 key。
	// 🔴 前缀 `hk-volatility` **在** `HK_DATA_QUERY_PREFIXES` 里: 数据取自本地
	// `kline_hk_index_daily`(VHSI + HSI 日K), 港股指数同步完必须失效 ——
	// 样本窗口一变, 分位阈值随之重算, 档位可能整个换档。
	// 🔴 纯本地读, 不打上游 ⇒ 不需要像分红/南向那样为"省上游请求"给足 staleTime,
	// 但仍建议给: 一次响应带 250 个绘图点, 切 tab 就重取没必要。
	hkVolatility: (seriesDays: number) => ['hk-volatility', seriesDays] as const,

	// 港股人气榜。无查询参数(上游硬上限前 100 名, 后端刻意不暴露分页) ⇒ 静态 key。
	// 🔴 前缀 `hk-hot-rank` **在** `HK_DATA_QUERY_PREFIXES` 里: 榜单本身来自上游,
	// 但**名称**左连本地 `instruments_hk` 维表 —— 维表同步完必须失效, 否则新上市
	// 标的会一直显示成 null 名称。
	// ⚠️ 别照抄 `hk-connect-flow`(那条刻意**不**在表里, 因为它不读任何本地数据)。
	// 判断标准是「有没有读本地数据」, 不是「是不是港股端点」。
	hkHotRank: ['hk-hot-rank'] as const,
	// 单标的人气详情。`symbol` 与 `withIntraday`(是否带当日盘中序列)都影响结果 ⇒ 入 key。
	// 🔴 前缀同为 `hk-hot-rank`, 故与列表一起被维表同步失效(名称同源)。
	hkHotRankDetail: (symbol: string, withIntraday: boolean) =>
		['hk-hot-rank', 'detail', symbol, withIntraday] as const,

	// A 股财务五张表。🔴 前缀 `financials` 与上面的 `hk-financials` 互斥。
	// 住在 queryKeys 而不是 lib/useFinancials.ts: 市场适配层
	// (lib/financial-market-adapter.ts) 也要拼这些 key, 而 useFinancials 反过来
	// 要 import 适配层 —— key 留在 hook 文件里会形成循环依赖。
	financialsStatus: ['financials', 'status'] as const,
	financialsMetrics: (symbol?: string) => ['financials', 'metrics', symbol] as const,
	financialsIncome: (symbol?: string) => ['financials', 'income', symbol] as const,
	financialsBalanceSheet: (symbol?: string) => ['financials', 'balance-sheet', symbol] as const,
	financialsCashFlow: (symbol?: string) => ['financials', 'cash-flow', symbol] as const,
	financialsShares: (symbol?: string) => ['financials', 'shares', symbol] as const,

	// Schema
	extDataSchemaAll: ['ext-data-schema-all'] as const,
	tableSchema: (table: string) => ['table-schema', table] as const,

	// Custom Signals
	customSignals: ['custom-signals'] as const,
	customSignalsOptions: ['custom-signals-options'] as const,

	// Monitor (监控规则 + 触发记录)
	monitorRules: ['monitor-rules'] as const,
	monitorRuleOptions: ['monitor-rule-options'] as const,
	lots: ['lots'] as const,
	lotsKline: (symbols: string) => ['lots-kline', symbols] as const,

	// 模拟盘 (多账户: 键按账户隔离; paperAll 作账户无关失效前缀)
	paperAll: ['paper'] as const,
	apiTokens: ['settings', 'api-tokens'] as const,
	paperAccounts: ['paper', 'accounts'] as const,
	paperOverview: (acc: string) => ['paper', 'overview', acc] as const,
	paperOrders: (acc: string) => ['paper', 'orders', acc] as const,
	paperTrades: (acc: string) => ['paper', 'trades', acc] as const,
	paperNav: (acc: string) => ['paper', 'nav', acc] as const,
	paperStats: (acc: string) => ['paper', 'stats', acc] as const,
	paperRounds: (acc: string) => ['paper', 'rounds', acc] as const,
	paperCompare: ['paper', 'compare'] as const,
	paperKillSwitch: ['paper', 'kill-switch'] as const,
	paperAutoRules: (acc: string) => ['paper', 'auto-rules', acc] as const,
	alerts: (source?: string) => ['alerts', source ?? ''] as const,

	// AI 大盘复盘
	reviewReports: ['review-reports'] as const,

	// 概念/行业涨幅轮动矩阵。🔴 market 进键: 两个市场的最新交易日常常相同, 键里
	// 漏掉 market 会让切市场后**继续显示上一个市场的矩阵** (react-query 命中缓存,
	// 连请求都不发) —— 症状是港股页面渲染 A 股概念, 且 5 分钟 staleTime 内不自愈。
	rpsRotation: (days: number, market: Market) => ['rps-rotation', days, market] as const,

	// 市场环境(Regime) — 日级离线计算, 不进 SSE 刷新
	// 历史查询键 = 实际请求三元组 (影响结果的参数全部进键): 'all' 在 coverage
	// 到达前后 start/end 不同 → 自动换键重拉, 不会把默认窗口数据当"全部"展示。
	regimeHistory: (range: { start?: string; end?: string; limit?: number }) =>
		['regime-history', range.start ?? '', range.end ?? '', range.limit ?? 0] as const,
	// 失效用裸前缀: 前缀匹配所有历史查询变体 (regimeHistory() 带哨兵值匹配不到任何键)
	regimeHistoryPrefix: ['regime-history'] as const,
	regimeLatest: ['regime-latest'] as const,
	regimeStates: (days: number) => ['regime-states', days] as const,
	regimeCoverage: ['regime-coverage'] as const,
	regimePhases: (start?: string, end?: string) => ['regime-phases', start ?? '', end ?? ''] as const,
	regimeMainline: (kind: string, start?: string, end?: string) => ['regime-mainline', kind, start ?? '', end ?? ''] as const,
	// 板块切换 (盘中轮动, 全量分钟聚合) — 30s 前端轮询刷新; seriesKey = 自定义展示板块清单,
	// filterKey = 自动活跃榜行数与排除名单 (会改变结果的参数必须进查询键)
	sectorRotation: (kind: string, market: Market, flow?: string, bucket?: number, seriesKey?: string, filterKey?: string) => ['sector-rotation', kind, market, flow ?? '', bucket ?? 5, seriesKey ?? '', filterKey ?? ''] as const,
	// 板块切换的指数叠加线 (核心四只, 默认上证) — 分钟取当日, 日K取昨收基准
	sectorRotationIndexMinute: (symbol: string, date?: string) => ['sector-rotation-index-minute', symbol, date ?? ''] as const,
	sectorRotationIndexDaily: (symbol: string) => ['sector-rotation-index-daily', symbol] as const,
	// 扩展表 schema 清单 (板块切换的资金流列选择器等)
	extSchemaAll: ['ext-schema-all'] as const,
} as const

// ===== SSE 应该 invalidate 的 key 前缀列表 =====
// 新增需要 SSE 推送的查询，只需在此加一行
//
// 注意: 策略页 (screener-cached) 不在此列表 —— 行情刷新时策略结果不变
// (非监控策略读盘后静态缓存, 监控策略由独立的 strategy_results_updated 事件在
// 重算完成后刷新)。若加入 'screener', 会导致每个行情 tick 双重刷新策略页,
// 且在 monitor "重算" 窗口内读到空结果, 造成策略列表闪烁 (变 0 → 空失效 → 又出现)。

export const SSE_INVALIDATE_PREFIXES = [
	// 精确前缀: 只命中自选页的实时数据 (quotes/enriched)。不能用宽泛的 'watchlist' ——
	// 会误伤 ['watchlist'] (自选列表) 和 ['watchlist-groups'] (分组配置, 只随手动操作变化)。
	// 旧设置里的 'watchlist' 单开关由 useQuoteStream 兼容读取。
	'watchlist-quotes',
	'watchlist-enriched',
	'quote-status',
	'index-quotes',
	'overview-market',
	'limit-ladder',
	// 概念/行业分析的全市场快照 (#419): 后端读最新日内缓存, 行情更新后需重取,
	// 否则停留页面时板块聚合数值一直停留旧值。仅两页打开时才实际发请求。
	'market-snapshot',
] as const
