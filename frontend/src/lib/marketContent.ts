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
 * - **accent 配色 / 空态文案**。等真有消费方(市场切换器、K线/自选/详情空态)再加,
 *   不预先堆没人读的字段。
 */
import { normalizeMarket, type Market } from '@/lib/market'

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
	/** 核心指数(顺序即展示顺序), 与后端 `index_const.py` 单一权威对齐。 */
	readonly coreIndexes: readonly CoreIndex[]
}

export const MARKET_CONTENT: Record<Market, MarketContent> = {
	CN: {
		market: 'CN',
		label: 'A股',
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
		coreIndexes: [
			{ symbol: 'HSI', name: '恒生指数', shortName: '恒生指数' },
			{ symbol: 'HSTECH', name: '恒生科技指数', shortName: '恒生科技' },
			{ symbol: 'HSCEI', name: '恒生中国企业指数', shortName: '国企指数' },
			{ symbol: 'HSCCI', name: '恒生香港中资企业指数', shortName: '中资企业' },
		],
	},
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
