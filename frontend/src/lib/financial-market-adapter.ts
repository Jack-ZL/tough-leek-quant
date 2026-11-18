/**
 * 财务概况页 — 市场适配层。
 *
 * 与 `index-market-adapter.ts` / `kline-market-adapter.ts` 同一条教条(方案 §8
 * 规则 2):「市场差异只写在适配器文件, 组件里禁止 `if (market === 'HK')`」。
 * `custom/financials/Page.tsx`、`components/financials/StockFinancialDetail.tsx`
 * 只认 :data:`FinancialMarketSource` 这一个接口; 新增市场 = 在 `FINANCIAL_SOURCES`
 * 加一项, 页面与详情组件零改动。
 *
 * **为什么港股走独立端点**, 而不是给 `/api/financials/*` 加 `market` 参数: 与日K、
 * 指数同理(见 `backend/app/api/hk.py` 模块 docstring) —— A 股那五条路由读
 * `financials/<table>/part.parquet`, 港股财务住在 `financials_hk/`, 传 `00700.HK`
 * 进去只会静默返回空。
 *
 * 🔴 **两个市场的表集与列集都不同, 不是「同一张表少几行」**:
 *
 * | | A 股 | 港股 |
 * |---|---|---|
 * | 表 | 5 张(metrics/income/balance_sheet/cash_flow/**shares**) | 4 张(**无股本表**) |
 * | 端点 | 一表一端点, 各回 `{data:[…]}` | **一个端点回全部四表** `{meta, tables:{…}}` |
 * | 列 | metrics 29 列 | metrics 26 列(A 股子集 11 + 港股独有 9 + 键 6) |
 * | 同步 | 每张表可单独同步, 有每表 `last_sync` | 整批重任务(job), 无每表时间 |
 * | 金额币种 | 一律人民币元 | **每家公司各自的编报币种**(CNY/HKD/USD 混存) |
 *
 * 所以字段表(`FIELD_DEFS`)必须按市场分开: 拿 A 股字段表去读港股行, 周转率/现金
 * 比率那 15 列会整片渲染成「—」, 看起来像「港股数据缺失」, 实际是**数据源不提供
 * 该口径** —— 正是方案 §8 说的「标题改了、口径没改, 比不切换更误导」。
 *
 * 🔴 **跨市场不得沿用缓存**: query key 前缀互斥(`financials` / `hk-financials`),
 * 且切市场时 symbol 也变(A 股 6 位 / 港股 5 位 + `.HK`), 双重隔离。
 */
import {
	api,
	type FinancialStatus,
	type HkFinancialsResponse,
	type HkFinancialsStatus,
} from '@/lib/api'
import { type Market } from '@/lib/market'
import { marketContent, symbolMarket, type MarketContent } from '@/lib/marketContent'
import { QK } from '@/lib/queryKeys'
import { type QueryObserverOptions } from '@tanstack/react-query'

/** 全部可能的财务表 tab(跨市场并集)。渲染哪些由 `source.tabs` 决定。 */
export const ALL_FINANCIAL_TABS = [
	'metrics', 'income', 'balance_sheet', 'cash_flow', 'shares',
] as const

export type FinancialTab = (typeof ALL_FINANCIAL_TABS)[number]

/**
 * 数值格式化类型。
 * - `pct` 存的是百分点(12.3 表示 12.3%)
 * - `amount` 金额(转万/亿/万亿)
 * - `perShare` 每股指标
 * - `num` 普通数值(2 位小数)
 * - `int` 整数(每手股数这类, 不能渲染成 100.00)
 */
export type FinancialFmt = 'pct' | 'amount' | 'perShare' | 'num' | 'int'

export interface FinancialFieldDef {
	readonly key: string
	readonly label: string
	readonly fmt: FinancialFmt
}

export interface FinancialRow {
	period_end?: string | null
	announce_date?: string | null

	[key: string]: any
}

/**
 * 标的级元信息。A 股端点不提供 ⇒ `null`(不是「空对象」, 读端据此判断该市场
 * 有没有这层信息)。
 */
export interface FinancialTableMeta {
	/** 该标的是否在本地已同步的数据里。false ⇒ 空态要说「这只没同步」而不是「整个市场没同步」。 */
	readonly synced: boolean
	/**
	 * 金额列的**编报币种**; 未知 → `null`。
	 * 🔴 不得默认人民币: 港股各公司编报币种不同(腾讯 CNY / 汇丰 HKD), 后端只在
	 * 利润表长表里能取到 `CURRENCY_CODE`, 长表未同步时就是未知。
	 */
	readonly reportCurrency: string | null
	/** 计价币种(股价/估值列用)。🔴 与编报币种**不必相同**。 */
	readonly pricingCurrency: string | null
	/** 后端下发的币种警示原文(不在前端另备一份文案)。 */
	readonly currencyWarning: string | null
	/** 一般企业 / 银行 / 证券 / 保险 —— 决定金融股字段族是否有值。 */
	readonly orgType: string | null
	readonly lotSize: number | null
	readonly latestReportDate: string | null
	readonly periods: number | null
}

export interface FinancialTableData {
	readonly rows: readonly FinancialRow[]
	readonly meta: FinancialTableMeta | null
}

/** 同步状态卡片(页面顶部那一排)。 */
export interface FinancialStatusCard {
	readonly key: string
	readonly label: string
	readonly rows: number
	/** 标的数; 该表不提供 → `null`(渲染「—」, 不是 0 —— 0 会被读成「同步过但没数据」)。 */
	readonly symbols: number | null
	/** 该表最近同步时间; 整批任务制市场没有每表时间 → `null`。 */
	readonly lastSync: string | null
}

/** 归一化后的同步状态 —— 两个市场的原始 payload 形状不同, 页面只认这一个。 */
export interface FinancialStatusView {
	/** 本地有无任何数据(驱动「暂无财务数据」空态)。 */
	readonly available: boolean
	/**
	 * 服务端自报的同步中状态。
	 * 🔴 整批任务制市场(港股)恒 `false` —— 它的进度住在 job store 里, 由页面的
	 * job 轮询驱动, 不在 status payload 里。
	 */
	readonly syncing: boolean
	readonly cards: readonly FinancialStatusCard[]
	/** 参与「全量同步」进度推算的表序; 无每表 `last_sync` 的市场为空数组。 */
	readonly syncOrder: readonly string[]
	/** 市场级摘要(港股: 最新报告期 / 每手股数覆盖 / 报告期类型)。 */
	readonly notes: readonly string[]
}

export interface FinancialSyncOutcome {
	/** 整批任务制市场: 需要轮询进度的 job id。 */
	readonly jobId?: string
	/** A 股: 后端是否真的开始了(防并发时会 `started=false` + reason)。 */
	readonly started?: boolean
	readonly reason?: string
}

/**
 * 财务页各能力对该市场**是否就绪**。
 *
 * 🔴 `false` 的语义是「入口直接隐藏」(方案 §8 规则 4), 不是「渲染出来但为空」:
 * 一个必然失败的按钮比没有按钮更糟。
 */
export interface FinancialCapabilities {
	/** 每张表可单独同步(A 股)。港股是整批重任务, 没有每表入口 ⇒ false。 */
	readonly perTableSync: boolean
	/** 同步走 job store, 需要轮询 `pipelineJob(id)` 显示进度(港股) ⇒ A 股 false。 */
	readonly jobSync: boolean
	/** 该市场有股本表 ⇒ 港股 false(宽表不提供股本口径, 不是「没同步」)。 */
	readonly sharesTable: boolean
	/**
	 * AI 财务分析入口。
	 * ✅ 两个市场都 true: 后端 `financial_analyzer` 已按 `infer_market(symbol)`
	 * 分派读取器与提示词口径(PR5), 港股走 `hk_latest_financials`。
	 */
	readonly aiAnalysis: boolean
}

export interface FinancialMarketSource {
	readonly market: Market
	readonly content: MarketContent
	readonly capabilities: FinancialCapabilities
	/** 渲染哪些 tab、按什么顺序。 */
	readonly tabs: readonly FinancialTab[]
	/** 页面副标题(说清这个市场有哪几张表)。 */
	readonly pageSubtitle: string
	/** 页头一行说明: 数据住在哪、与别的市场是否混算、币种口径。 */
	readonly headerHint: string
	readonly searchPlaceholder: string
	/** 搜索框下方的示例提示。 */
	readonly searchExample: string
	/** 顶部主同步按钮文案。 */
	readonly syncAllLabel: string
	/** 状态卡片区的列数类名(卡片数不同, 栅格也不同)。 */
	readonly cardGridClass: string

	tabLabel(tab: FinancialTab): string
	fieldDefs(tab: FinancialTab): readonly FinancialFieldDef[]
	/** 该 symbol 是否属于本市场(用于「上次查看」跨市场恢复时把关)。 */
	ownsSymbol(symbol: string): boolean
	statusQuery(): FinancialStatusQuery
	tableQuery(tab: FinancialTab, symbol?: string): FinancialTableQuery
	/** 空态即契约(方案 §8 规则 3): 必须解释**为什么**空 + 给出**出路**。 */
	tableEmptyHint(tab: FinancialTab, symbol?: string): string
	/** 金额单位/币种说明; 该市场无歧义 → `null`(不渲染, 而不是渲染一行废话)。 */
	amountNote(meta: FinancialTableMeta | null): string | null
	/** 机构类型提示(港股金融股专有口径); 无此概念 → `null`。 */
	orgTypeHint(meta: FinancialTableMeta | null): string | null
	/** 报告期旁的补充标签(港股: 年报/中报/一季报/三季报); 无 → `null`。 */
	periodBadge(row: FinancialRow): string | null
	syncTable(table: string): Promise<FinancialSyncOutcome>
	/** 同步成功后要失效的 query key。 */
	readonly invalidateKeys: readonly (readonly unknown[])[]
}

// useQuery 的入参: 原始响应类型各市场不同, 归一化后统一是 FinancialStatusView /
// FinancialTableData。用 QueryObserverOptions 而不是 UseQueryOptions 是因为要显式
// 声明「原始 → select 后」两个不同的数据类型。
export type FinancialStatusQuery = QueryObserverOptions<
	any, Error, FinancialStatusView, any, readonly unknown[]
>
export type FinancialTableQuery = QueryObserverOptions<
	any, Error, FinancialTableData, any, readonly unknown[]
>

// ── A 股 ──────────────────────────────────────────────────────────────
const CN_TAB_LABELS: Record<FinancialTab, string> = {
	metrics: '核心指标',
	income: '利润表',
	balance_sheet: '资产负债表',
	cash_flow: '现金流量表',
	shares: '股本',
}

const CN_TABS: readonly FinancialTab[] = [
	'metrics', 'income', 'balance_sheet', 'cash_flow', 'shares',
]

/** 全量同步的表序(与后端 `/api/financials/sync/all` 的推进顺序一致)。 */
const CN_SYNC_ORDER: readonly string[] = [
	'metrics', 'income', 'balance_sheet', 'cash_flow', 'shares',
]

/**
 * A 股字段表 —— 从 `StockFinancialDetail.tsx` 原样搬来, **一字未改**。
 * 搬家的理由: 它现在是「A 股这一市场的口径」, 与港股字段表并列住在适配层,
 * 组件不再持有市场专属内容(方案 §8 规则 2)。
 */
const CN_FIELD_DEFS: Record<FinancialTab, readonly FinancialFieldDef[]> = {
	metrics: [
		{ key: 'eps_basic', label: '基本每股收益 EPS', fmt: 'perShare' },
		{ key: 'eps_diluted', label: '稀释每股收益 EPS', fmt: 'perShare' },
		{ key: 'bps', label: '每股净资产 BPS', fmt: 'perShare' },
		{ key: 'ocfps', label: '每股经营现金流', fmt: 'perShare' },
		{ key: 'roe', label: '净资产收益率 ROE', fmt: 'pct' },
		{ key: 'roe_diluted', label: '稀释 ROE', fmt: 'pct' },
		{ key: 'roa', label: '总资产收益率 ROA', fmt: 'pct' },
		{ key: 'gross_margin', label: '销售毛利率', fmt: 'pct' },
		{ key: 'net_margin', label: '销售净利率', fmt: 'pct' },
		{ key: 'debt_to_asset_ratio', label: '资产负债率', fmt: 'pct' },
		{ key: 'revenue_yoy', label: '营业收入同比增长', fmt: 'pct' },
		{ key: 'net_income_yoy', label: '净利润同比增长', fmt: 'pct' },
		{ key: 'operating_cash_to_revenue', label: '经营现金/营收', fmt: 'pct' },
		{ key: 'inventory_turnover', label: '存货周转率', fmt: 'num' },
	],
	income: [
		{ key: 'revenue', label: '营业收入', fmt: 'amount' },
		{ key: 'operating_cost', label: '营业成本', fmt: 'amount' },
		{ key: 'operating_profit', label: '营业利润', fmt: 'amount' },
		{ key: 'selling_expense', label: '销售费用', fmt: 'amount' },
		{ key: 'admin_expense', label: '管理费用', fmt: 'amount' },
		{ key: 'rd_expense', label: '研发费用', fmt: 'amount' },
		{ key: 'financial_expense', label: '财务费用', fmt: 'amount' },
		{ key: 'non_operating_income', label: '营业外收入', fmt: 'amount' },
		{ key: 'non_operating_expense', label: '营业外支出', fmt: 'amount' },
		{ key: 'total_profit', label: '利润总额', fmt: 'amount' },
		{ key: 'income_tax', label: '所得税', fmt: 'amount' },
		{ key: 'net_income', label: '净利润', fmt: 'amount' },
		{ key: 'net_income_attributable', label: '归母净利润', fmt: 'amount' },
		{ key: 'net_income_deducted', label: '扣非净利润', fmt: 'amount' },
		{ key: 'basic_eps', label: '基本每股收益', fmt: 'perShare' },
		{ key: 'diluted_eps', label: '稀释每股收益', fmt: 'perShare' },
	],
	balance_sheet: [
		{ key: 'total_assets', label: '资产总计', fmt: 'amount' },
		{ key: 'total_current_assets', label: '流动资产合计', fmt: 'amount' },
		{ key: 'total_non_current_assets', label: '非流动资产合计', fmt: 'amount' },
		{ key: 'cash_and_equivalents', label: '货币资金', fmt: 'amount' },
		{ key: 'accounts_receivable', label: '应收账款', fmt: 'amount' },
		{ key: 'inventory', label: '存货', fmt: 'amount' },
		{ key: 'fixed_assets', label: '固定资产', fmt: 'amount' },
		{ key: 'intangible_assets', label: '无形资产', fmt: 'amount' },
		{ key: 'goodwill', label: '商誉', fmt: 'amount' },
		{ key: 'total_liabilities', label: '负债合计', fmt: 'amount' },
		{ key: 'total_current_liabilities', label: '流动负债合计', fmt: 'amount' },
		{ key: 'total_non_current_liabilities', label: '非流动负债合计', fmt: 'amount' },
		{ key: 'short_term_borrowing', label: '短期借款', fmt: 'amount' },
		{ key: 'long_term_borrowing', label: '长期借款', fmt: 'amount' },
		{ key: 'accounts_payable', label: '应付账款', fmt: 'amount' },
		{ key: 'total_equity', label: '所有者权益合计', fmt: 'amount' },
		{ key: 'equity_attributable', label: '归母所有者权益', fmt: 'amount' },
		{ key: 'retained_earnings', label: '未分配利润', fmt: 'amount' },
		{ key: 'minority_interest', label: '少数股东权益', fmt: 'amount' },
	],
	cash_flow: [
		{ key: 'net_operating_cash_flow', label: '经营活动现金流净额', fmt: 'amount' },
		{ key: 'net_investing_cash_flow', label: '投资活动现金流净额', fmt: 'amount' },
		{ key: 'net_financing_cash_flow', label: '筹资活动现金流净额', fmt: 'amount' },
		{ key: 'capex', label: '固定资产/无形资产投资', fmt: 'amount' },
		{ key: 'net_cash_change', label: '现金及等价物净增加额', fmt: 'amount' },
	],
	shares: [
		{ key: 'total_shares', label: '总股本', fmt: 'amount' },
		{ key: 'float_shares', label: '流通股本', fmt: 'amount' },
	],
}

/** A 股一表一端点。key 必须与 `lib/useFinancials.ts` 的 `FINANCIAL_QK` 逐字一致。 */
const CN_TABLE_REQUESTS: Record<
	FinancialTab,
	(symbol?: string) => { queryKey: readonly unknown[]; queryFn: () => Promise<{ data?: any[] }> }
> = {
	metrics: s => ({ queryKey: QK.financialsMetrics(s), queryFn: () => api.financialMetrics(s) }),
	income: s => ({ queryKey: QK.financialsIncome(s), queryFn: () => api.financialIncome(s) }),
	balance_sheet: s => ({ queryKey: QK.financialsBalanceSheet(s), queryFn: () => api.financialBalanceSheet(s) }),
	cash_flow: s => ({ queryKey: QK.financialsCashFlow(s), queryFn: () => api.financialCashFlow(s) }),
	shares: s => ({ queryKey: QK.financialsShares(s), queryFn: () => api.financialShares(s) }),
}

function cnStatusView(s: FinancialStatus | undefined): FinancialStatusView {
	const tables = s?.tables ?? {}
	const lastSync = s?.last_sync ?? {}
	return {
		available: s?.available ?? false,
		syncing: s?.syncing ?? false,
		cards: CN_SYNC_ORDER.map(key => ({
			key,
			label: CN_TAB_LABELS[key as FinancialTab],
			rows: tables[key]?.rows ?? 0,
			symbols: tables[key]?.symbols ?? 0,
			lastSync: lastSync[key] ?? null,
		})),
		syncOrder: CN_SYNC_ORDER,
		notes: [],
	}
}

const CN_SOURCE: FinancialMarketSource = {
	market: 'CN',
	content: marketContent('CN'),
	capabilities: { perTableSync: true, jobSync: false, sharesTable: true, aiAnalysis: true },
	tabs: CN_TABS,
	pageSubtitle: '利润表 / 资负表 / 现金流 / 关键指标 / 股本 / AI分析',
	headerHint: '',
	searchPlaceholder: '输入股票代码或名称，如 600000 / 浦发',
	searchExample: '支持股票代码或名称模糊匹配，如 600000 / 浦发',
	syncAllLabel: '全部同步',
	cardGridClass: 'grid-cols-1 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-5',
	tabLabel: tab => CN_TAB_LABELS[tab],
	fieldDefs: tab => CN_FIELD_DEFS[tab],
	ownsSymbol: symbol => symbolMarket(symbol) === 'CN',
	statusQuery: () => ({
		queryKey: QK.financialsStatus,
		queryFn: () => api.financialStatus(),
		select: cnStatusView,
		staleTime: 60_000,
		// 同步进行中时每 3s 轮询, 及时反映表数变化与同步完成; 空闲时不轮询。
		// 🔴 回调拿到的是 **select 之前**的原始数据(Query 实例存的是原始响应)。
		refetchInterval: query => (query.state.data?.syncing ? 3_000 : false),
	}),
	tableQuery: (tab, symbol) => {
		const req = CN_TABLE_REQUESTS[tab](symbol)
		return {
			queryKey: req.queryKey,
			queryFn: req.queryFn,
			enabled: !!symbol,
			staleTime: 300_000,
			// A 股端点不提供标的级元信息 ⇒ meta 恒 null(读端据此不渲染币种行)。
			select: (d: any) => ({ rows: d?.data ?? [], meta: null }),
		}
	},
	tableEmptyHint: tab => `暂无${CN_TAB_LABELS[tab]}数据 — 可点击顶部「全部同步」拉取`,
	// A 股金额一律人民币元, 无歧义 ⇒ 不加说明行(保持既有 A 股界面不变)。
	amountNote: () => null,
	orgTypeHint: () => null,
	periodBadge: () => null,
	async syncTable(table) {
		const r = await api.financialSync(table)
		return { started: r.synced?.started, reason: r.synced?.reason }
	},
	invalidateKeys: [['financials']],
}

// ── 港股 ──────────────────────────────────────────────────────────────
const HK_TAB_LABELS: Record<FinancialTab, string> = {
	metrics: '核心指标',
	income: '利润表',
	balance_sheet: '资产负债表',
	cash_flow: '现金流量表',
	// 港股没有这张表; 标签仍要给全(Record 是全表), 但不会出现在 HK_TABS 里。
	shares: '股本',
}

/**
 * 🔴 港股**没有股本表**: 东财港股主指标宽表不提供股本口径, 不是「没同步」。
 * 渲染一个必然为空的 tab 会让用户以为同步失败(方案 §8 规则 4: 入口直接隐藏)。
 */
const HK_TABS: readonly FinancialTab[] = ['metrics', 'income', 'balance_sheet', 'cash_flow']

/**
 * 港股字段表 —— 只列**后端投影确实带出**的列(权威: `HK_CANONICAL_PROJECTION`
 * 11/5/3/3 项 + `HK_ONLY_CANONICAL_FIELDS` 9 项)。
 *
 * 🔴 刻意**不**把 A 股字段表拿来「缺的显示 —」: 港股宽表确实不提供周转率/现金
 * 比率族(实测 A 股 metrics 29 列 vs 港股 26 列, 其中 15 列 A 股独有), 整片渲染
 * 成「—」会被读成数据缺失。缺的口径由 AI 提示词与空态文案说明, 不占表格行。
 *
 * 标签用港式/IFRS 表述(后端 `GET /api/hk/financials/statement` docstring 明确要求
 * 前端不得复用 A 股科目标签)。
 */
const HK_FIELD_DEFS: Record<FinancialTab, readonly FinancialFieldDef[]> = {
	metrics: [
		{ key: 'eps_basic', label: '基本每股盈利 EPS', fmt: 'perShare' },
		{ key: 'bps', label: '每股资产净值 BPS', fmt: 'perShare' },
		{ key: 'roe', label: '股东资金回报率 ROE(加权平均)', fmt: 'pct' },
		{ key: 'roa', label: '总资产回报率 ROA', fmt: 'pct' },
		{ key: 'gross_margin', label: '毛利率', fmt: 'pct' },
		{ key: 'net_margin', label: '净利率', fmt: 'pct' },
		{ key: 'current_ratio', label: '流动比率', fmt: 'num' },
		{ key: 'debt_to_asset_ratio', label: '资产负债率', fmt: 'pct' },
		{ key: 'revenue_yoy', label: '营业额同比', fmt: 'pct' },
		{ key: 'net_income_yoy', label: '股东应占溢利同比', fmt: 'pct' },
		{ key: 'operating_cash_to_revenue', label: '经营现金/营业额', fmt: 'pct' },
		// 估值三列 🔴 是**港币**口径, 与金额列的编报币种不同(见 amountNote)。
		{ key: 'pe_ttm', label: '市盈率 PE(TTM·港币)', fmt: 'num' },
		{ key: 'pb_ttm', label: '市净率 PB(TTM·港币)', fmt: 'num' },
		{ key: 'total_market_cap', label: '总市值(港币)', fmt: 'amount' },
		{ key: 'lot_size', label: '每手股数', fmt: 'int' },
	],
	income: [
		{ key: 'revenue', label: '营业额', fmt: 'amount' },
		{ key: 'operating_profit', label: '经营溢利', fmt: 'amount' },
		{ key: 'total_profit', label: '除税前溢利', fmt: 'amount' },
		{ key: 'net_income_attributable', label: '股东应占溢利', fmt: 'amount' },
		{ key: 'basic_eps', label: '基本每股盈利', fmt: 'perShare' },
	],
	balance_sheet: [
		{ key: 'total_assets', label: '资产总计', fmt: 'amount' },
		{ key: 'total_liabilities', label: '负债合计', fmt: 'amount' },
		{ key: 'total_equity', label: '股东权益合计(归母)', fmt: 'amount' },
	],
	cash_flow: [
		{ key: 'net_operating_cash_flow', label: '经营活动现金流净额', fmt: 'amount' },
		{ key: 'net_investing_cash_flow', label: '投资活动现金流净额', fmt: 'amount' },
		{ key: 'net_financing_cash_flow', label: '融资活动现金流净额', fmt: 'amount' },
	],
	shares: [],
}

/** 后端 `GET /api/hk/financials` 的 limit 上限是 80; 财务页看最近几期即可。 */
const HK_FIN_LIMIT = 8

/** 金融股机构类型(后端 `hk_financials.py` 实测取值: 一般企业/银行/证券/保险)。 */
const HK_FINANCIAL_ORG_TYPES = new Set(['银行', '证券', '保险'])

function hkMeta(d: HkFinancialsResponse | undefined): FinancialTableMeta | null {
	if (!d) return null
	return {
		synced: !!d.synced,
		reportCurrency: d.report_currency ?? null,
		pricingCurrency: d.pricing_currency ?? null,
		currencyWarning: d.currency_warning ?? null,
		orgType: d.org_type ?? null,
		lotSize: d.lot_size ?? null,
		periods: d.periods ?? null,
		latestReportDate: d.latest_report_date ?? null,
	}
}

function hkStatusView(s: HkFinancialsStatus | undefined): FinancialStatusView {
	const long = s?.tables ?? {}
	const notes: string[] = []
	if (s?.synced) {
		if (s.latest_report_date) notes.push(`最新报告期 ${s.latest_report_date}`)
		if (s.lot_size_coverage != null) notes.push(`每手股数已覆盖 ${s.lot_size_coverage} 只`)
		if (s.period_names?.length) notes.push(`报告期类型 ${s.period_names.join(' / ')}`)
	}
	return {
		available: s?.synced ?? false,
		// 🔴 港股同步是 job(重任务槽), status payload 里没有 syncing 字段;
		// 进度由页面的 job 轮询驱动(capabilities.jobSync = true)。
		syncing: false,
		cards: [
			{ key: 'main_indicator', label: '主指标宽表', rows: s?.rows ?? 0, symbols: s?.symbols ?? 0, lastSync: null },
			{ key: 'income', label: '利润表长表', rows: long.income ?? 0, symbols: null, lastSync: null },
			{ key: 'balance', label: '资负表长表', rows: long.balance ?? 0, symbols: null, lastSync: null },
		],
		// 整批任务没有每表 last_sync ⇒ 不参与「第几张表完成」推算。
		syncOrder: [],
		notes,
	}
}

/** 该市场没有这张表时的停用查询(never fetched, 只为满足 hooks 数量恒定)。 */
const UNAVAILABLE_TABLE: FinancialTableQuery = {
	queryKey: ['financial-table-unavailable'],
	queryFn: () => Promise.resolve({ rows: [], meta: null }),
	enabled: false,
}

const HK_SOURCE: FinancialMarketSource = {
	market: 'HK',
	content: marketContent('HK'),
	capabilities: { perTableSync: false, jobSync: true, sharesTable: false, aiAnalysis: true },
	tabs: HK_TABS,
	pageSubtitle: '利润表 / 资负表 / 现金流 / 关键指标 / AI分析',
	headerHint: '港股财务与 A 股分开存储（financials_hk/），需单独同步；四张表都由主指标宽表投影，一次同步即全部就绪。港股无股本表（数据源不提供该口径）。',
	searchPlaceholder: '输入港股代码或名称，如 00700 / 腾讯',
	searchExample: '支持港股代码或名称模糊匹配，如 00700 / 腾讯',
	syncAllLabel: '同步港股财务',
	cardGridClass: 'grid-cols-1 sm:grid-cols-2 md:grid-cols-3',
	tabLabel: tab => HK_TAB_LABELS[tab],
	fieldDefs: tab => HK_FIELD_DEFS[tab],
	ownsSymbol: symbol => symbolMarket(symbol) === 'HK',
	statusQuery: () => ({
		queryKey: QK.hkFinancialsStatus,
		queryFn: () => api.hkFinancialsStatus(),
		select: hkStatusView,
		staleTime: 60_000,
	}),
	tableQuery: (tab, symbol) => {
		// 能力位为 false 的表直接停用(入口也在 tabs 里被摘掉, 这里是第二道保险)。
		if (!HK_TABS.includes(tab)) return UNAVAILABLE_TABLE
		return {
			// 🔴 四个 tab **共用一个 key**: 后端一次返回全部四表, 各 tab 用 select
			// 各取一片 ⇒ react-query 自动去重成一次请求, 组件仍保持「一 tab 一 hook」。
			queryKey: QK.hkFinancials(symbol ?? '', HK_FIN_LIMIT),
			queryFn: () => api.hkFinancials(symbol ?? '', { limit: HK_FIN_LIMIT }),
			enabled: !!symbol,
			staleTime: 300_000,
			select: (d: HkFinancialsResponse) => ({ rows: d?.tables?.[tab] ?? [], meta: hkMeta(d) }),
		}
	},
	tableEmptyHint: (_tab, symbol) =>
		`港股财务里还没有 ${symbol ?? '该标的'} 的数据。点顶部「同步港股财务」拉取主指标宽表（四张表都从它投影，一次同步即全部就绪）；若刚同步过仍为空，说明该标的不在东财港股财务覆盖范围内。`,
	amountNote: (meta) => {
		if (!meta) return null
		// 🔴 币种文案以后端 `currency_warning` 为准, 前端不另备一份(见 marketContent
		// 文件头「货币不在前端备一份」)。这里只补上后端取到的**具体**编报币种。
		const cur = meta.reportCurrency
			? `本标的编报币种 ${meta.reportCurrency}。`
			: '本标的编报币种未知（需同步利润表长表才能取到 CURRENCY_CODE）。'
		return `${cur}${meta.currencyWarning ?? ''}`
	},
	orgTypeHint: (meta) => {
		const t = meta?.orgType
		if (!t || !HK_FINANCIAL_ORG_TYPES.has(t)) return null
		return `金融股（${t}）：毛利率、存货周转等一般企业口径为空是数据源口径，不是缺数；资产负债率天然偏高，不适用一般企业阈值。`
	},
	// 港股报告期类型必须显示: 财年结日不固定在年末(阿里 6 月底是「一季报」),
	// 且中报是**审阅**数不可年化 —— 光看 period_end 会误判。
	periodBadge: row => (typeof row?.period_name === 'string' && row.period_name ? row.period_name : null),
	async syncTable() {
		const r = await api.hkSyncFinancials({})
		return { jobId: r.job_id, started: true }
	},
	invalidateKeys: [['hk-financials']],
}

export const FINANCIAL_SOURCES: Record<Market, FinancialMarketSource> = {
	CN: CN_SOURCE,
	HK: HK_SOURCE,
}

/**
 * `financialSource` 的非空版: 入参已经是 `Market`(不是 `Market | null`)时必有源。
 *
 * 存在的理由: hook 里 `useMutation`/`useQuery` 不能条件调用, 拿 `FinancialMarketSource
 * | null` 就得写 `source!` 非空断言。`Record<Market, …>` 的下标访问在类型上就是
 * 非空的, 用它代替断言 —— 新增市场忘了注册会在**编译期**报错, 而不是运行时炸。
 */
export function requireFinancialSource(market: Market): FinancialMarketSource {
	return FINANCIAL_SOURCES[market]
}

/**
 * 取某市场的财务数据源; 市场未知 → `null`。
 *
 * 🔴 **刻意不兜底 CN**(与 `indexSource` 相反, 与 `klineSource` 同): 本函数决定
 * 打哪个端点、用哪套字段表, 属**请求层**。按 `lib/market.ts` 文件头的表格, 请求层
 * 兜底会把港股代码送到 A 股端点静默返回空, 或把 A 股字段表套到港股行上渲染出
 * 一整片「—」—— 都比当场报错难发现得多。
 */
export function financialSource(market: Market | null): FinancialMarketSource | null {
	return market == null ? null : FINANCIAL_SOURCES[market]
}
