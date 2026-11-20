// 板块类页面 (概念板块 / 行业板块) 的各市场维度源与无源空态文案。
//
// 为什么单独一个模块: 两个板块页是**同构的自定义扩展页** (`custom/concept-analysis`、
// `custom/industry-analysis`), 无源结论也来自同一次实测 (方案 §3.4)。文案若各写一份,
// 将来找到港股板块源时必然只改一处 —— 另一处继续谎称「无源」。
//
// 🔴 文案**不进** `lib/marketContent.ts`: 那张表只放市场差异**事实** (label/货币/时区/
// 核心指数/代码形态), 「为什么没有数据」是数据源侧知识 (marketContent.ts 头注同口径,
// 与两个适配器的 `emptyHint`/`dailyEmptyHint` 分工一致)。
//
// 🔴 用 `Record<Market, …>` 而不是 `market === 'HK'` 字面量分支: 新增市场时这里会
// **编译报错**, 逼作者显式回答「这个市场有没有板块源」, 而不是静默落进 else 把 A 股
// 板块当港股渲染 (方案 §8 规则 2; 同构范式见 `components/StockDetailBody.tsx` 的 BODIES)。
import type { Market } from './market'

export type BoardKind = 'concept' | 'industry'

/**
 * 板块维度 (概念名 / 行业名) 从哪来。两个市场的**通道不同**, 但下游
 * `DimensionGroup[]` 契约相同 —— 差异只在这一层收敛, 页面主体不按市场分支。
 */
export type BoardDimensionSource =
	/** 扩展数据表 (A 股: `ext_gn_ths` 同花顺概念 / `ext_hy_ths` 同花顺行业) */
	| { kind: 'ext-data' }
	/**
	 * 行情快照自带的分类列 (港股行业: `industry` = 恒生行业)。
	 * 港股没有对应的扩展数据表, 分类只能随 `/api/screener/market-snapshot?market=HK`
	 * 下发 —— 数据本来就在 `instruments_hk` 里 (东财 f100), 且被
	 * `instrument_sync_hk._HK_PRESERVE_FIELDS` 专门保护不被上游抖动掏空。
	 *
	 * `label` 是这套分类的**人类名字**, 给页面副标题/空态用。放在数据里而不是页面里
	 * 写死「恒生行业」, 是为了守住 CONTRIBUTING §3.5「市场差异是数据不是控制流」:
	 * 将来别的市场也走快照通道时, 只加一行表项, 不用再改页面。
	 */
	| { kind: 'snapshot-field'; field: string; label: string }

/**
 * `null` = 该市场该板块**无源**, 整页只渲染空态文案 (CONTRIBUTING §4 fail closed:
 * 宁可明说没有, 不可用 A 股数据冒充)。
 *
 * 🔴 港股**行业有源、概念无源**, 两者结论不同不是笔误:
 * - 行业: `instruments_hk.industry` (恒生行业) 已随港股维表同步落库, 是本地真数据。
 * - 概念: 东财板块中心的概念板块实测全是 A 股, akshare 港股侧只有人气榜, 港股维表
 *   也没有概念列 —— 确实无源, 继续 fail closed。
 * 早先这里对两者一律判「无源」, 是把「东财板块中心没有港股」误当成「港股没有行业
 * 分类」; 本模块头注承诺的「找到源时只改一处」正是为此而设。
 */
export const BOARD_DIMENSION_SOURCE: Record<BoardKind, Record<Market, BoardDimensionSource | null>> = {
	concept: {
		CN: { kind: 'ext-data' },
		HK: null,
	},
	industry: {
		CN: { kind: 'ext-data' },
		HK: { kind: 'snapshot-field', field: 'industry', label: '恒生行业' },
	},
}

export interface BoardNoSourceCopy {
	title: string
	hint: string
}

/**
 * 无源空态文案。**只在 `BOARD_DIMENSION_SOURCE` 为 null 的格子里非 null** ——
 * 两张表的一致性由 `board-market-support.test.ts` 钉住, 不靠人记。
 */
export const BOARD_NO_SOURCE_COPY: Record<BoardKind, Record<Market, BoardNoSourceCopy | null>> = {
	concept: {
		CN: null,
		HK: {
			title: '港股无概念板块数据源',
			hint: HK_NO_SOURCE_HINT('概念'),
		},
	},
	industry: {
		CN: null,
		HK: null,
	},
}

/** 取某板块页在某市场的维度源; 无源时返回 null。 */
export function boardDimensionSource(kind: BoardKind, market: Market): BoardDimensionSource | null {
	return BOARD_DIMENSION_SOURCE[kind][market]
}

/**
 * 涨幅轮动对话框 (`RpsRotationDialog`) 在各市场渲染哪些控件、用什么文案。
 *
 * 🔴 这是后端能力位的**前端镜像**, 不是「有没有数据」的判据 —— 有没有数据一律由
 * 后端 fail-closed 决定, 前端只决定**控件与文案**。对应关系:
 *
 * | 字段 | 后端事实来源 |
 * |---|---|
 * | `industryLevels` | A 股行业名是 "一级-二级-三级" 路径 (`rps_rotation._matrix_from_joined` 按 `-` 拆层); 恒生行业是单层分类, 无 `-` 可拆 |
 * | `aiAnalysis` | `MarketTradingRules.has_market_overview` —— AI 分析要装配大盘背景 (涨停统计 / 概念行业排行), 目前只有 A 股口径 |
 *
 * 用表而不是 `market === 'HK'`: 新增市场时 `Record<Market, …>` 会**编译报错**,
 * 逼作者显式回答, 而不是静默落进 else 把 A 股控件渲染给别的市场 (同文件头注)。
 */
export interface RotationSupport {
	/** 是否渲染 1/2/3 级行业层级选择器 */
	industryLevels: boolean
	/** 是否渲染 AI 轮动分析区块 */
	aiAnalysis: boolean
	/** `aiAnalysis=false` 时替代整个 AI 区块的说明; `aiAnalysis=true` 时为 null */
	aiAnalysisHint: string | null
	/** 矩阵为空时的空态文案。入参是维度中文名 (「概念」/「行业」) */
	emptyHint: (dimLabel: string) => string
}

export const ROTATION_SUPPORT: Record<Market, RotationSupport> = {
	CN: {
		industryLevels: true,
		aiAnalysis: true,
		aiAnalysisHint: null,
		emptyHint: dimLabel =>
			`暂无${dimLabel}数据,请先在「${dimLabel}板块」页配置并获取${dimLabel}数据源`,
	},
	HK: {
		industryLevels: false,
		aiAnalysis: false,
		aiAnalysisHint:
			'AI 轮动分析需要大盘背景（涨停统计 / 概念行业排行），目前只有 A 股口径，'
			+ '港股暂不支持 —— 不会用 A 股背景冒充港股（CONTRIBUTING §4 fail closed）。'
			+ '下方轮动矩阵是港股本地真数据，可直接查看。',
		emptyHint: dimLabel =>
			`暂无港股${dimLabel}轮动数据 — 港股${dimLabel}分类随行情快照下发，`
			+ '请先在「数据 → 港股数据」完成维表与日K同步',
	},
}

/** 取某市场轮动对话框的控件/文案能力。 */
export function rotationSupport(market: Market): RotationSupport {
	return ROTATION_SUPPORT[market]
}

/** 取某板块页在某市场的空态文案; 有源时返回 null。 */
export function boardNoSourceCopy(kind: BoardKind, market: Market): BoardNoSourceCopy | null {
	// 有源就不给空态文案 —— 以维度源表为准, 避免文案表忘了同步时整页被误判成无源。
	if (boardDimensionSource(kind, market) !== null) return null
	return BOARD_NO_SOURCE_COPY[kind][market]
}

// 实测结论 (方案 §3.4): 东财板块中心 `push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:{1,2,3}`
// 返回 31 地域 / 496 行业 / 554 概念, **全是 A 股** (BK0882 猪肉概念 等); `t:4/t:5/t:6`
// 返回空。akshare `stock_board.html` 里港股只有**人气榜**, 板块接口全是
// `stock_board_concept_*_em` / `stock_board_industry_*_em` / `*_ths` (A 股)。
// 东财网页版同样没有港股板块。
function HK_NO_SOURCE_HINT(kindLabel: string): string {
	return `已实测确认：东财板块中心的地域/行业/概念三类板块全部只覆盖 A 股，akshare 港股侧仅提供人气榜，`
		+ `东财网页版同样没有港股板块分类。因此本页不渲染任何港股${kindLabel}数据，也不会用 A 股${kindLabel}冒充`
		+ `（CONTRIBUTING §4 fail closed：宁可明说没有，不可显示假数据）。`
		+ `${kindLabel}成分走扩展数据通道，将来若找到港股${kindLabel}源，在「数据 → 扩展数据」配一张表即可接入，无需改代码。`
		+ `查看 A 股${kindLabel}板块请用侧栏市场切换器切回 A 股。`
}
