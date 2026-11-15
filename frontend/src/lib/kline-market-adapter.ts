/**
 * 个股详情(日K) — 市场适配层。
 *
 * 与 `index-market-adapter.ts` 同一条教条(方案 §8 规则 2):「市场差异只写在适配器
 * 文件, 组件里禁止 `if (market === 'HK')`」。详情弹窗
 * (`components/StockPreviewDialogContent.tsx`)与图表本体
 * (`components/StockDailyKChart.tsx`)都只认 :data:`KlineMarketSource` 这一个接口;
 * 新增市场 = 在 `KLINE_SOURCES` 加一项 + 在 `components/StockDetailBody.tsx` 的
 * 主体表里加一行, 弹窗与图表零改动。
 *
 * **为什么港股走独立端点**, 而不是给 `/api/kline/daily` 加 `market` 参数: 与指数页
 * 同理(见 `backend/app/api/hk.py` 模块 docstring) —— A 股那条路由绑着实时行情链路
 * (quote lease / SSE / 最新一根增量 / 财务 / 涨跌停标记), 港股一样都没有, 传
 * `00700.HK` 进去只会静默返回空。两个端点的**行字段**都是 `KlineRow`, 所以图表
 * 组件仍然共用, 差异只在「打哪个端点、用哪个 query key、空态说什么」。
 *
 * 🔴 **排序**: A 股 `/api/kline/daily` 与港股 `/api/hk/daily` **都是升序**。别把
 * 指数页那条「港股端点倒序, 适配器里显式排回来」的经验推广成「港股都倒序」——
 * 港股**个股**端点在设计时就对齐了 A 股(倒序喂 ECharts 会画出左右镜像的 K 线)。
 * 真正倒序的是 `/api/hk/index_daily`, 归 `index-market-adapter.ts` 管。
 *
 * 🔴 **跨市场不得沿用缓存**: query key 前缀互斥(`kline` / `hk-daily`), 且港股侧的
 * `placeholderData` 守卫**同时**比 key 前缀与 symbol。只比 symbol 的话, 同一个
 * `useQuery` 实例从 A 股切到港股时会把 A 股的行当占位数据先显示出来 —— 两个市场
 * 的复权口径与成交量纲都不同, 那正是方案 §8 引用的「标题改了、口径没改, 比不
 * 切换更误导」。
 */
import { api } from '@/lib/api'
import { klineDailyQueryOptions, type KlineDailyQueryOptions } from '@/lib/kline'
import { type Market } from '@/lib/market'
import { marketContent, marketToday, type MarketContent } from '@/lib/marketContent'
import { QK } from '@/lib/queryKeys'

/**
 * 详情弹窗里哪些能力对该市场**已就绪**。
 *
 * 🔴 `false` 的语义是「入口直接隐藏」(方案 §8 规则 4), 不是「渲染出来但为空」:
 * 一个必然失败的按钮比没有按钮更糟 —— 用户点了自选, 后端 `watchlist.py` 零市场
 * 感知会把港股代码当 A 股存进去, 然后列表页永远拉不到它的行情。
 */
export interface KlineCapabilities {
	/** 分时(分钟K)链路。港股无实时行情源(方案 §3.4) ⇒ false。 */
	readonly minute: boolean
	/** 加入/移出自选。后端 `api/watchlist.py` 目前零市场感知 ⇒ 港股 false。 */
	readonly watchlist: boolean
	/** 监控规则与点位提醒。监控引擎只跑 A 股信号 ⇒ 港股 false。 */
	readonly monitor: boolean
	/** 异动边缘(全市场 A 股累计偏离计算) ⇒ 港股 false, 且不发起那次重计算。 */
	readonly abnormal: boolean
}

/** 代码前的板块小标(A 股 创/科/北)。 */
export interface BoardTag {
	readonly label: string
	readonly color: string
}

export interface KlineMarketSource {
	readonly market: Market
	readonly content: MarketContent
	readonly capabilities: KlineCapabilities
	/**
	 * 日K查询配置, 注入 `StockDailyKChart` 的 `queryOptions` 插槽。
	 * 两个市场各自的端点与 query key 都在这里, 图表本体不认市场。
	 */
	dailyQueryOptions(symbol: string, range: { start: string; end: string }): KlineDailyQueryOptions
	/**
	 * 空态文案(空态即契约, 方案 §8 规则 3): 必须解释**为什么**空 + 给出**出路**。
	 * `emptyReason` 取自响应字段, 不得在前端猜。
	 */
	dailyEmptyHint(emptyReason?: string | null): string
	/** 刷新按钮要失效的 query key(按当前视图)。 */
	refreshKeys(symbol: string, view: 'daily' | 'intraday'): readonly (readonly unknown[])[]
	/** 代码前的板块小标; 该市场无此概念 → `null`(不渲染, 而不是渲染一个空徽章)。 */
	boardTag(symbol: string): BoardTag | null
	/** 该市场的「今天」, 用于滚动日期范围的右端。 */
	today(now?: Date): string
}

/** A 股板块小标(与 Screener 列表一致)。按 6 位代码前缀判板块。 */
function cnBoardTag(symbol: string): BoardTag | null {
	if (/^(300|301)/.test(symbol)) return { label: '创', color: 'text-[#f97316] bg-[#f97316]/12 border-[#f97316]/25' }
	if (/^688/.test(symbol))       return { label: '科', color: 'text-purple-400 bg-purple-400/12 border-purple-400/25' }
	if (/^[48]/.test(symbol))      return { label: '北', color: 'text-cyan-400 bg-cyan-400/12 border-cyan-400/25' }
	return null
}

const CN_SOURCE: KlineMarketSource = {
	market: 'CN',
	content: marketContent('CN'),
	capabilities: { minute: true, watchlist: true, monitor: true, abnormal: true },
	dailyQueryOptions: (symbol, range) => klineDailyQueryOptions(symbol, range),
	dailyEmptyHint: () => '暂无日K数据。可以先同步日K，或调整日期范围。',
	refreshKeys: (symbol, view) => view === 'daily'
		? [['kline', symbol]]
		: [['kline-minute-range', symbol], ['kline-minute', symbol]],
	boardTag: cnBoardTag,
	today: now => marketToday('CN', now),
}

/** 后端 `GET /api/hk/daily` 的 limit 上限是 5000; 半年~1年窗口远用不满, 取 500。 */
const HK_DAILY_LIMIT = 500

const HK_SOURCE: KlineMarketSource = {
	market: 'HK',
	content: marketContent('HK'),
	// 港股没有实时行情链路(方案 §3.4): 分时、最新一根增量、盘中轮询都不存在。
	// 自选与监控的后端目前零市场感知(见 KlineCapabilities 文档), 属**未就绪**而非
	// 「不支持」—— 后端补齐后把对应开关翻成 true 即可, 弹窗代码不用动。
	capabilities: { minute: false, watchlist: false, monitor: false, abnormal: false },
	dailyQueryOptions: (symbol, range) => ({
		queryKey: QK.hkDaily(symbol, range.start, range.end, HK_DAILY_LIMIT),
		queryFn: () => api.hkDaily(symbol, {
			start: range.start, end: range.end, limit: HK_DAILY_LIMIT,
		}),
		// 🔴 比 key 前缀 + symbol 两件事, 见文件头「跨市场不得沿用缓存」。
		placeholderData: (prev: any, prevQuery: any) => {
			const prevKey = prevQuery?.queryKey as readonly unknown[] | undefined
			return prevKey?.[0] === 'hk-daily' && prevKey?.[1] === symbol ? prev : undefined
		},
	}),
	dailyEmptyHint: (emptyReason) => emptyReason === 'no_enriched'
		? '港股日K已同步，但前复权指标(enriched)尚未生成 —— 本端点刻意不回退原始价，因为原始价跨除净日算出的均线会静默失真。请到「数据」页重算港股 enriched；若重算后仍为空，说明该标的复权因子不完整，被完整性凭据判为拒算(详见 coverage.json 的 uncovered 清单)。'
		: '港股日K尚未同步。请到「数据」页依次同步港股维表、日K，再重算 enriched。',
	refreshKeys: symbol => [['hk-daily', symbol]],
	// 🔴 港股**不能**沿用 A 股的板块判定: `/^[48]/` 在 A 股是北交所, 而港股 8 开头
	// 是创业板 —— 直接复用会给港股代码挂上「北」字标。港股 5 位代码前缀不承载
	// 板块语义, 如实返回 null(入口隐藏, 不编一个)。
	boardTag: () => null,
	today: now => marketToday('HK', now),
}

export const KLINE_SOURCES: Record<Market, KlineMarketSource> = {
	CN: CN_SOURCE,
	HK: HK_SOURCE,
}

/**
 * 取某市场的日K数据源; 市场未知 → `null`。
 *
 * 🔴 **刻意不兜底 CN**(与 `indexSource` 相反): 本函数决定打哪个端点, 属**请求层**。
 * 按 `lib/market.ts` 文件头的表格, 请求层兜底会把港股代码送到 A 股端点, 静默返回
 * 空 —— 比当场报错难发现得多。调用方(详情弹窗)的 symbol 已经过 `symbolMarket`
 * 把关, 正常路径拿不到 `null`; 拿到就说明有人绕过了那道关, 应当显式处理。
 */
export function klineSource(market: Market | null): KlineMarketSource | null {
	return market == null ? null : KLINE_SOURCES[market]
}
