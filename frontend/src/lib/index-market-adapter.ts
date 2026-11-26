/**
 * 指数页 — 市场适配层。
 *
 * 方案 §8 规则 2:「市场差异只写在适配器文件, 组件里禁止 `if (market === 'HK')`」。
 * 页面(`pages/Indices.tsx`)只认 :data:`IndexMarketSource` 这一个接口, 两个市场各
 * 实现一份; 新增市场 = 在 `INDEX_SOURCES` 加一项, 页面零改动。
 *
 * **为什么港股走独立端点**, 而不是给 `/api/intraday/indices` 加 `market` 参数:
 * 那条路由绑全局 QuoteService 缓存, 兜底读的是 A 股指数目录, 传 `HSI` 会静默返回空。
 * 两个端点的响应形状对齐(`rows`/`count`/`source`), 所以渲染逻辑仍然共用。
 *
 * 🔴 **两个市场的日K排序相反**: A 股 `/api/index/daily` 升序; 港股
 * `/api/hk/index_daily` 是 `ORDER BY date DESC`。图表与「取最后一根当最新日」都假设
 * 升序, 故本适配层统一保证**升序输出**(港股侧显式排序)。漏掉这步 K 线会左右镜像。
 *
 * 🔴 **跨市场不得沿用缓存**: 两个市场的 query key 前缀不同(`index-*` / `hk-index-*`),
 * 页面据此在切市场时丢弃 `placeholderData` —— 否则港股页会短暂显示上证指数的数字,
 * 正是方案 §8 引用的「标题改了、口径没改, 比不切换更误导」。
 */
import { api, type IndexQuote } from '@/lib/api'
import { DEFAULT_MARKET, normalizeMarket, type Market } from '@/lib/market'
import { coreIndexes, marketContent, type CoreIndex, type MarketContent } from '@/lib/marketContent'
import { QK } from '@/lib/queryKeys'

/**
 * 图表消费的日K行 —— 两个市场响应字段的并集。
 * 港股指数没有指标列(后端只存 OHLCV, 指标读时即时算), 缺的字段自然为 `undefined`,
 * `toOHLC` 逐字段判空后填 `null`。
 */
export interface IndexChartRow {
	symbol?: string
	date: string
	open?: number | null
	high?: number | null
	low?: number | null
	close?: number | null
	volume?: number | null
	ma5?: number | null
	ma10?: number | null
	ma20?: number | null
	ma60?: number | null
	macd_dif?: number | null
	macd_dea?: number | null
	macd_hist?: number | null
	rsi_6?: number | null
	rsi_14?: number | null
	rsi_24?: number | null
	kdj_k?: number | null
	kdj_d?: number | null
	kdj_j?: number | null
	boll_upper?: number | null
	boll_lower?: number | null
}

export interface IndexQuotesResult {
	rows: IndexQuote[]
	count: number
	/** 后端返回的数据源标签; `null` = 该端点不提供。 */
	source: string | null
	/**
	 * 延时分钟数; `null` = 实时。
	 * 🔴 只能来自接口字段 `quote_delay_minutes`, 不得在前端写死。
	 */
	quoteDelayMinutes: number | null
}

export interface IndexDailyResult {
	symbol: string
	name: string | null
	/** 升序(见文件头)。 */
	rows: IndexChartRow[]
	source: string | null
}

export interface IndexMarketSource {
	readonly market: Market
	readonly content: MarketContent
	/** 页面固定展示的核心指数(产品契约, 不提供全指数搜索)。 */
	readonly pinned: readonly CoreIndex[]
	/** 该市场是否有指数分时端点。港股没有 —— 腾讯/新浪只提供日K。 */
	readonly supportsMinute: boolean
	/** 页头短标语 (PageHeader subtitle 首段, 全站统一的「A · B」句式)。 */
	readonly headerTagline: string
	/** 页面副标题: 说清数据住在哪、与别的市场是否混算。 */
	readonly headerHint: string
	/** 分时不可用时的原因(空态即契约, 方案 §8 规则 3)。 */
	readonly minuteUnavailableHint: string
	/** 日K空态文案: 必须解释为什么 + 给出路。 */
	readonly dailyEmptyHint: string
	readonly syncLabel: string
	readonly quotesKey: readonly unknown[]
	/**
	 * 拉取指数快照。`symbols` 可选, 供只需要清单子集的调用方收窄请求 (侧栏指数条
	 * 只要核心四只)。🔴 **由各市场自己决定怎么用它**, 调用方不做市场分支:
	 * A 股端点支持 `?symbols=` 过滤; 港股端点本来就只请求核心四只, 传不传等价
	 * (故 HK 实现直接不声明该形参)。缺省 = 该市场的全量口径(与指数页一致)。
	 */
	fetchQuotes(symbols?: readonly string[]): Promise<IndexQuotesResult>
	dailyKey(symbol: string, start: string, end: string): readonly unknown[]
	fetchDaily(symbol: string, range: { start: string; end: string }): Promise<IndexDailyResult>
	syncDaily(): Promise<unknown>
	/** 同步成功后要失效的 query key。 */
	readonly invalidateKeys: readonly (readonly unknown[])[]
	/** 行情来源标注(方案 §8 规则 5: 数据源标签必须真实, 降级要如实说)。 */
	quoteSourceLabel(data: IndexQuotesResult | undefined): string
}

/** 港股快照只请求核心四只 —— 白名单全集 27 只里其余的不是页面展示对象。 */
const HK_PINNED = coreIndexes('HK')
const HK_SYMBOLS = HK_PINNED.map(i => i.symbol)
const HK_QUOTES_KEY = HK_SYMBOLS.join(',')
/** 后端 `GET /api/hk/index_daily` 的 limit 上限内取默认值; 半年窗口远用不满。 */
const HK_DAILY_LIMIT = 500

const CN_SOURCE: IndexMarketSource = {
	market: 'CN',
	content: marketContent('CN'),
	pinned: coreIndexes('CN'),
	supportsMinute: true,
	headerTagline: '核心指数行情 · 日K与分时',
	headerHint: '指数使用独立 kline_index_* parquet，不进入股票策略链路。',
	minuteUnavailableHint: '分钟K(批量)数据不可用',
	dailyEmptyHint: '暂无日K数据。可以先同步指数日K，或选择其他指数。',
	syncLabel: '同步指数日K',
	quotesKey: QK.indexQuotes,
	async fetchQuotes(symbols) {
		// 侧栏指数条只要核心四只 ⇒ 透传收窄请求; 指数页不传, 保持既有全量口径不变。
		const r = await api.indexQuotes(symbols?.length ? [...symbols] : undefined)
		// A 股是实时行情, 无延时字段 ⇒ null(徽章不渲染)。
		return { rows: r.rows ?? [], count: r.count ?? 0, source: null, quoteDelayMinutes: null }
	},
	dailyKey: (symbol, start, end) => QK.indexDaily(symbol, start, end),
	async fetchDaily(symbol, range) {
		const r = await api.indexDaily(symbol, 180, range)
		return {
			symbol: r.symbol,
			name: r.name ?? r.index_info?.name ?? null,
			rows: r.rows ?? [],
			source: r.source ?? null,
		}
	},
	syncDaily: () => api.syncIndexDaily(365),
	invalidateKeys: [QK.indexQuotes, ['index-daily']],
	quoteSourceLabel: data => `实时缓存 ${data?.count ?? 0} 只指数`,
}

const HK_SOURCE: IndexMarketSource = {
	market: 'HK',
	content: marketContent('HK'),
	pinned: HK_PINNED,
	supportsMinute: false,
	// 🔴 静态标语只描述「有哪些数据」, 不能出现「延时快照」四个字: 那是 quoteSourceLabel
	// 专用于 source==='realtime' 的**状态**措辞, 快照降级时必须消失 (契约见
	// Indices.market.test.tsx「快照降级时如实标注, 不伪装成实时」)。故这里写「延时行情」。
	headerTagline: '核心指数行情 · 日K与延时行情',
	headerHint: '港股指数使用独立 kline_hk_index_* parquet，不与 A 股混算；行情为交易所授权的延时数据。',
	minuteUnavailableHint: '港股指数无分时数据源（腾讯/新浪仅提供日K）',
	dailyEmptyHint: '港股指数日K未就绪。点右上角「同步港股指数日K」拉取；若已同步仍为空，说明该指数不在腾讯日K覆盖范围内（27 只白名单中仅 14 只有日K），实时快照仍可用。',
	syncLabel: '同步港股指数日K',
	quotesKey: QK.hkIndexQuotes(HK_QUOTES_KEY),
	async fetchQuotes() {
		const r = await api.hkIndices(HK_SYMBOLS)
		return {
			rows: r.rows ?? [],
			count: r.count ?? 0,
			source: r.source ?? null,
			quoteDelayMinutes: r.quote_delay_minutes ?? null,
		}
	},
	dailyKey: (symbol, start, end) => QK.hkIndexDaily(symbol, start, end, HK_DAILY_LIMIT),
	async fetchDaily(symbol, range) {
		const r = await api.hkIndexDaily(symbol, {
			start: range.start, end: range.end, limit: HK_DAILY_LIMIT,
		})
		const rows: IndexChartRow[] = r.rows ?? []
		// 🔴 端点是 ORDER BY date DESC, 图表假设升序 ⇒ 显式排回(见文件头)。
		rows.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0))
		// name 仅核心四只能解析(名称由快照响应字段 1 在运行时给, 不落库), 其余为 null。
		return { symbol: r.symbol, name: r.name ?? null, rows, source: null }
	},
	syncDaily: () => api.hkSyncIndexDaily(),
	invalidateKeys: [QK.hkIndexQuotes(HK_QUOTES_KEY), ['hk-index-daily']],
	quoteSourceLabel: (data) => {
		const n = data?.count ?? 0
		if (data?.source === 'index_daily') return `${n} 只指数 · 快照不可用，已降级读本地日K最新一根`
		if (data?.source === 'realtime') return `${n} 只指数 · 延时快照`
		return `${n} 只指数 · 来源未知`
	},
}

export const INDEX_SOURCES: Record<Market, IndexMarketSource> = {
	CN: CN_SOURCE,
	HK: HK_SOURCE,
}

/** 取某市场的指数数据源。未知 market 兜底 CN(仅渲染用, 见 `lib/market.ts`)。 */
export function indexSource(market?: unknown): IndexMarketSource {
	return INDEX_SOURCES[normalizeMarket(market)]
}

/**
 * 指数页 URL 参数。缺省市场不写进 URL —— 保持既有 A 股链接(`/indices?symbol=…`,
 * 侧栏指数条就在用)形态不变。
 */
export function indexPageParams(market: Market, symbol?: string): Record<string, string> {
	const params: Record<string, string> = {}
	if (market !== DEFAULT_MARKET) params.market = market
	if (symbol) params.symbol = symbol
	return params
}
