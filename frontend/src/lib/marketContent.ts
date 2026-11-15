/**
 * 市场差异的**唯一居所**(方案 §8 规则 2:「市场差异只写在适配器文件, 组件里禁止
 * `if (market === 'HK')`」)。新增市场 = 在 `MARKET_CONTENT` 加一行, 不改任何组件。
 *
 * 反面教材(QuantMind 实证, 见方案 §8): fork 页面导致两个文件 1013/1010 行互相复制、
 * diff 291 行双向漂移、**切到美股渲染的是 A 股页面**; 以及「标题改了、口径没改,
 * 比不切换更误导」。所以差异集中在本文件, 页面全线共用。
 *
 * 🔴 **刻意不放在这张表里**的东西(放进来就是埋雷):
 *
 * - **延时分钟数**。必须读接口返回的 `quote_delay_minutes`(港股两个指数端点都带),
 *   不得在前端写死 15 —— 后端 `rules_for(Market.HK).quote_delay_minutes` 才是单一
 *   权威, 写死会在源侧改延时后静默说谎。见 `QuoteDelayBadge`。
 * - **货币**。`/api/hk/*` 响应自带 `currency`, 同理不在前端备一份。
 * - **涨跌幅颜色语义 / 涨跌停 / 交易时段**。这些是**规则**不是文案, 归后端
 *   `MarketTradingRules`; 前端 `lib/price-limit.ts` 那套 A 股涨跌停推算对港股
 *   是**编造**(港股无涨跌停), 港股链路不得调用。
 * - **accent 配色**。等真有消费方(全局市场切换器)再加, 不预先堆没人读的字段。
 * - **空态文案**。它确实有消费方, 但**不住在这里**: 文案要跟着「打哪个端点、
 *   后端回的 `empty_reason` 是哪一态」走, 那是请求层知识, 归两个适配器
 *   (`lib/index-market-adapter.ts` 的 `emptyHint` / `lib/kline-market-adapter.ts`
 *   的 `dailyEmptyHint`)。本表只放**与数据链路无关**的展示内容(名称、时区、
 *   代码格式、核心指数), 否则新增一个端点就要回来改这张表。
 *
 * 🟡 **例外: `symbolPattern` 与 `timeZone` 是规则, 但必须在这里备一份**。判据是
 * 「前端能否等后端回答」: 打开详情弹窗前要当场决定这个 symbol 属于哪个市场
 * (过滤 / 请求层**不许兜底**, 见 `lib/market.ts`), 算日期范围右端要当场知道市场
 * 时区 —— 两者都发生在**发请求之前**, 没有后端可问。所以它们是后端权威
 * (`market._SYMBOL_PATTERN_BODY` / `MarketTradingRules.iana_timezone`)的**只读镜像**,
 * 由 `marketContent.test.ts` 钉住逐字一致; 其余规则(涨跌停、交易时段、延时分钟数)
 * 前端都能在响应里拿到, 一律不许镜像。
 */
import { MARKETS, normalizeMarket, type Market } from '@/lib/market'

export interface CoreIndex {
	/** 指数代码, 与后端 `index_const.py` 逐字一致。港股为源生形(`HSI`), 不带 `.HK`。 */
	readonly symbol: string
	/** 权威中文名, 与后端 `index_const.py` 逐字一致。 */
	readonly name: string
	/**
	 * 窄处展示用的短名。侧栏指数条两列布局下单卡内容区仅 85px, 4 字中文名 +
	 * 涨跌幅已接近占满(曾出现「上证指/数」折行), 故长名必须给短名。
	 *
	 * 🔴 **同一市场内必须唯一**: `custom/review/Page.tsx` 的缩写表曾把
	 * 「恒生科技指数」和「恒生中国企业指数」都压成「恒生」, 两条并列显示同一个词。
	 * 由 `marketContent.test.ts` 钉住唯一性与长度。
	 */
	readonly shortName?: string
}

export interface MarketContent {
	readonly market: Market
	/** 市场中文名, 用于切换器与标注。 */
	readonly label: string
	/**
	 * IANA 时区, 与后端 ``MarketTradingRules.iana_timezone`` 逐字一致。
	 *
	 * 🔴 别因为「A 股和港股都是 UTC+8」就共用一个常量: 那是**巧合**而不是契约,
	 * 下一个市场(美股 UTC-5)会静默继承 A 股时区, 表现为「傍晚打开详情, 日期范围
	 * 的右端是明天」。取「今天」一律经 :func:`marketToday`。
	 */
	readonly timeZone: string
	/**
	 * 该市场 symbol 的**已锚定**正则, 与后端 ``market._SYMBOL_PATTERN_BODY`` 逐字对齐
	 * (A 股 6 位数字 + 交易所白名单; 港股 5 位数字 + ``.HK``)。
	 *
	 * 🔴 必须用**交易所后缀白名单**而不是宽松的 ``[A-Z]{2}``: 后者会把 ``007000.HK``
	 * 判成合法 A 股并流进 A 股链路(后端同一条注释)。
	 * ⚠️ 不得加 ``g`` 标志 —— ``RegExp.test`` 带 ``g`` 会推进 ``lastIndex``,
	 * 同一个正则对象第二次调用就漏判。
	 */
	readonly symbolPattern: RegExp
	/** 核心指数(顺序即展示顺序), 与后端 `index_const.py` 单一权威对齐。 */
	readonly coreIndexes: readonly CoreIndex[]
}

export const MARKET_CONTENT: Record<Market, MarketContent> = {
	CN: {
		market: 'CN',
		label: 'A股',
		timeZone: 'Asia/Shanghai',
		symbolPattern: /^[0-9]{6}\.(?:BJ|SH|SZ)$/,
		coreIndexes: [
			{ symbol: '000001.SH', name: '上证指数' },
			{ symbol: '399001.SZ', name: '深证成指' },
			{ symbol: '399006.SZ', name: '创业板指' },
			{ symbol: '000680.SH', name: '科创综指' },
		],
	},
	HK: {
		market: 'HK',
		label: '港股',
		timeZone: 'Asia/Hong_Kong',
		symbolPattern: /^[0-9]{5}\.HK$/,
		coreIndexes: [
			{ symbol: 'HSI', name: '恒生指数', shortName: '恒生指数' },
			{ symbol: 'HSTECH', name: '恒生科技指数', shortName: '恒生科技' },
			{ symbol: 'HSCEI', name: '恒生中国企业指数', shortName: '国企指数' },
			{ symbol: 'HSCCI', name: '恒生香港中资企业指数', shortName: '中资企业' },
		],
	},
}

/**
 * symbol → 市场。**过滤 / 请求层专用**: 认不出就返回 `null`, 绝不兜底 CN
 * (见 `lib/market.ts` 文件头表格 —— 把港股代码当 A 股查会静默返回空, 比报错难发现)。
 *
 * 🔴 判定只看**显式交易所后缀**, 不按数字位数猜市场。位数差(6 / 5)只是当前两个
 * 市场的巧合, 按位数判会在代码规则变化时静默串市场。
 *
 * 遍历 `MARKETS` 而不是写 `if (symbol.endsWith('.HK'))`: 新增市场只改上面的表。
 */
export function symbolMarket(symbol: unknown): Market | null {
	if (typeof symbol !== 'string') return null
	const text = symbol.trim().toUpperCase()
	for (const market of MARKETS) {
		if (MARKET_CONTENT[market].symbolPattern.test(text)) return market
	}
	return null
}

/**
 * 某市场的「今天」(YYYY-MM-DD)。与后端 ``market_today(market)`` 同口径。
 *
 * ⚠️ 不用 ``toISOString().slice(0, 10)`` —— 那是 UTC 日期, 东八区傍晚会错一天。
 */
export function marketToday(market: Market, now = new Date()): string {
	return new Intl.DateTimeFormat('en-CA', { timeZone: marketContent(market).timeZone }).format(now)
}

/** 取某市场的展示内容。未知 market 兜底 CN(仅渲染用, 见 `lib/market.ts` 文件头)。 */
export function marketContent(market?: unknown): MarketContent {
	return MARKET_CONTENT[normalizeMarket(market)]
}

/** 某市场的核心指数清单。 */
export function coreIndexes(market?: unknown): readonly CoreIndex[] {
	return marketContent(market).coreIndexes
}

/** 窄处展示名: 有短名用短名, 否则用权威全名。 */
export function indexDisplayName(index: CoreIndex): string {
	return index.shortName ?? index.name
}
