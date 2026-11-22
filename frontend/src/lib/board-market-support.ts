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
 * 概念页专属的对照句: 点破「港股行业有源、概念无源」这层区别。
 *
 * 🔴 为什么必须说: 用户往往刚从**行业板块**页切过来, 而那一页在港股态是正常渲染恒生行业的。
 * 只说「东财板块中心只覆盖 A 股」会被读成自相矛盾 —— 恒生行业恰恰**也来自东财**(维表 f100),
 * 只是走的不是板块中心那个端点。不点破, 用户要么以为行业页的数据是假的, 要么以为概念页坏了。
 *
 * 只挂在概念页: 行业页在港股态**有源**, `boardNoSourceCopy('industry','HK')` 恒为 null,
 * 根本渲染不到无源文案, 挂上去就是死代码。
 */
const HK_CONCEPT_VS_INDUSTRY_NOTE =
	'港股的行业分类不在此列：恒生行业走维表（东财 f100）而非板块中心，行业页在港股态可正常使用。'

/**
 * 无源空态文案。**只在 `BOARD_DIMENSION_SOURCE` 为 null 的格子里非 null** ——
 * 两张表的一致性由 `board-market-support.test.ts` 钉住, 不靠人记。
 */
export const BOARD_NO_SOURCE_COPY: Record<BoardKind, Record<Market, BoardNoSourceCopy | null>> = {
	concept: {
		CN: null,
		HK: {
			title: '港股无概念板块数据源',
			hint: HK_NO_SOURCE_HINT('概念', HK_CONCEPT_VS_INDUSTRY_NOTE),
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

/**
 * 「行业估值温度计」入口在各市场是否渲染 (方案 §10 恒生行业四件套第 2 件)。
 *
 * 🔴 只有港股为 `true`, 且这**不是**「A 股无源」的意思 —— 两者结论不同, 别混:
 * - 港股: 温度计建在**分红**上 (TTM 股息率 + 派息覆盖率), 源是东财
 *   `RPT_HKF10_MAIN_DIVBASIC` 全市场按除净日窗口取, 已在 PR7 打通。方案特别注明
 *   「港股是红利市场, 这个比涨幅排名更有用」, 所以它是港股专属维度。
 * - A 股: 行业估值走的是另一套源 (申万/同花顺行业 × 中证/东财板块估值), 与港股
 *   分红口径**不可共用一张表**; 目前尚未接入, 故不渲染入口。
 *
 * ⚠️ A 股态**刻意不给「尚未接入」提示**: 用户没在找一个他不曾见过的功能, 挂一句
 * 免责声明只是噪音。这与 `BOARD_NO_SOURCE_COPY` 的分工不同 —— 那边是用户**看得见
 * 一整页空掉**、必须解释; 这边只是一个按钮不出现。
 *
 * 用表而不是 `market === 'HK'`: 新增市场时 `Record<Market, …>` 会**编译报错**,
 * 逼作者显式回答, 而不是静默落进 else (同文件头注 / 方案 §8 规则 2)。
 */
export const SECTOR_VALUATION_THERMOMETER: Record<Market, boolean> = {
	CN: false,
	HK: true,
}

/** 取某市场是否渲染行业估值温度计入口。 */
export function sectorValuationThermometer(market: Market): boolean {
	return SECTOR_VALUATION_THERMOMETER[market]
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
//
// 🔴 但「东财没有港股板块分类」这句话是**错的**, 别写进文案: 港股行业分类来自东财
// `clist/get?fs=m:128` 的 **f100 恒生行业**(见 services/instrument_sync_hk.py), PR6-1 起
// 已接通。准确的口径是「东财**板块中心**(BK 板块)只覆盖 A 股」, 而不是「东财没有港股分类」。
// 早先的文案写了后半句, 与行业页的真实数据自相矛盾, 已删。
//
// `extraNote` 插在「不冒充」与「后路」之间: 收尾那句「切回 A 股」是行动指引, 必须留在最后。
//
// 🔴 文案长度是有约束的: `EmptyState` 把 hint 渲染成**单个 `<p>`**, 容器 `max-w-md`(≈448px),
// 中文一行约 30 字。超过 ~210 字就是十几行无断行的墙, 用户不会读。所以这里只留用户需要的
// 四件事(无源依据 / 不冒充 / 行业页可用 / 怎么回 A 股), **接入改法写在下面这段注释里而不是
// 文案里** —— 那是维护者信息, 而且 `BOARD_DIMENSION_SOURCE` 是源码标识符, 不该出现在 UI。
//
// 接入港股概念源的正确改法 (文案里那句「改法见注释」指的就是这里):
//   1. 把 `BOARD_DIMENSION_SOURCE.concept.HK` 从 `null` 改成 `{ kind: 'ext-data' }`;
//      这一步**不可省** —— 只要它是 null, `boardNoSourceCopy` 就返回非 null,
//      `concept-analysis/Page.tsx` 会在发任何查询之前提前 return, 配了扩展表也点不亮本页。
//   2. 把 `BOARD_NO_SOURCE_COPY.concept.HK` 改回 `null` (两表一致性由测试钉住, 漏改会红)。
//   3. 在「数据 → 扩展数据」配一张 symbol/concept 表。
//   页面主体、查询链、渲染层都不用动 —— 这才是方案 §589「架构上无需改动」的准确含义。
function HK_NO_SOURCE_HINT(kindLabel: string, extraNote = ''): string {
	return `实测确认：东财板块中心的地域/行业/概念三类板块全部只覆盖 A 股，akshare 港股侧仅提供人气榜，`
		+ `因此本页不渲染港股${kindLabel}数据，也不会用 A 股${kindLabel}冒充（CONTRIBUTING §4 fail closed）。`
		+ extraNote
		+ `将来可经扩展数据通道接入港股${kindLabel}源（改法见本文件注释）。`
		+ `查看 A 股${kindLabel}板块请用侧栏市场切换器切回 A 股。`
}
