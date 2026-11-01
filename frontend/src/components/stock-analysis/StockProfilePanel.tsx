/**
 * 个股资料面板 (P1-13) — 第一批: 限售解禁/股权质押/股票回购/股东户数;
 * 第二批: 十大股东/基金持仓/新股申购/业绩快报; 第三批: 资金流向(新浪)/逐笔成交(腾讯)。
 *
 * 数据来自免费源(东财 datacenter / 新浪 MoneyFlow / 腾讯 detail), 后端按需拉取 + 分级 JSON 缓存
 * (资料类 12h / 资金流 30min / 逐笔 60s);
 * 拉取失败后端降级(source_unavailable / stale), 本组件只做提示不打挂页面。
 * 挂在个股分析页看板下方; 未选标的时不渲染(零足迹)。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Clock, Database, Loader2, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import type {
	StockProfileDataset, StockProfileResponse,
	ProfileUnlockRow, ProfilePledgeRow, ProfileRepurchaseRow, ProfileHolderNumberRow,
	ProfileTopHoldersRow, ProfileFundHoldingRow, ProfileIpoInfoRow, ProfilePerfExpressRow,
	ProfileMoneyFlowRow, ProfileTickTradeRow,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtBigNum, priceColorClass } from '@/lib/format'

// ---- 每个数据集的列定义: key 对应后端归一化行字段, fmt 决定渲染方式 ----
// dec = 保留两位小数(EPS/发行价/PE 这类小数值, 不能走 fmtBigNum 取整)
type Fmt = 'date' | 'num' | 'dec' | 'pct' | 'spct' | 'text'

interface Col {
	key: string
	label: string
	fmt: Fmt
	/** 悬停提示(单位口径等) */
	title?: string
}

// spct = 带符号百分数(红涨绿跌着色); num 走 fmtBigNum(自动 万/亿)
const COLUMNS: Record<StockProfileDataset, Col[]> = {
	unlock: [
		{ key: 'free_date', label: '解禁日期', fmt: 'date' },
		{ key: 'free_shares_wan', label: '解禁股数(万)', fmt: 'num', title: '本次解禁股数, 单位万股' },
		{ key: 'free_market_cap_wan', label: '解禁市值(万)', fmt: 'num', title: '按解禁日收盘价估算, 单位万元' },
		{ key: 'ratio_of_total_pct', label: '占总股本%', fmt: 'pct', title: '本次解禁股数占总股本比例' },
		{ key: 'free_shares_type', label: '解禁类型', fmt: 'text' },
		{ key: 'holder_num', label: '涉及户数', fmt: 'num' },
	],
	pledge: [
		{ key: 'trade_date', label: '交易日', fmt: 'date', title: '中登公司周频统计口径' },
		{ key: 'pledge_ratio_pct', label: '质押比例%', fmt: 'pct', title: '质押股份占总股本比例' },
		{ key: 'pledge_shares_wan', label: '质押余额(万)', fmt: 'num', title: '质押股份数量, 单位万股' },
		{ key: 'pledge_market_cap_wan', label: '质押市值(万)', fmt: 'num' },
		{ key: 'pledge_deal_num', label: '质押笔数', fmt: 'num' },
	],
	repurchase: [
		{ key: 'notice_date', label: '公告日', fmt: 'date' },
		{ key: 'progress', label: '进度', fmt: 'text' },
		{ key: 'amount', label: '回购金额', fmt: 'num', title: '已实施回购金额, 单位元(预案阶段为空)' },
		{ key: 'num', label: '回购数量', fmt: 'num', title: '已实施回购股数' },
		{ key: 'price_cap', label: '价格上限', fmt: 'num', title: '回购方案价格上限, 元/股' },
		{ key: 'end_date', label: '截止日期', fmt: 'date' },
		{ key: 'objective', label: '目的', fmt: 'text' },
	],
	holder_number: [
		{ key: 'end_date', label: '截止日', fmt: 'date' },
		{ key: 'holder_num', label: '股东户数', fmt: 'num' },
		{ key: 'holder_num_change', label: '较上期', fmt: 'num', title: '较上一统计期变化户数(减少=筹码集中)' },
		{ key: 'holder_num_ratio_pct', label: '变化%', fmt: 'spct' },
		{ key: 'avg_hold_num', label: '户均持股', fmt: 'num' },
		{ key: 'avg_market_cap', label: '户均市值', fmt: 'num', title: '单位元' },
		{ key: 'interval_chrate_pct', label: '区间涨跌%', fmt: 'spct', title: '两期统计日之间股价涨跌幅' },
		{ key: 'notice_date', label: '公告日', fmt: 'date' },
	],
	// ---- P1-13 第二批 ----
	top_holders: [
		{ key: 'end_date', label: '截止日', fmt: 'date' },
		{ key: 'rank', label: '#', fmt: 'num' },
		{ key: 'holder_name', label: '股东名称', fmt: 'text' },
		{ key: 'hold_num', label: '持股数', fmt: 'num', title: '单位股' },
		{ key: 'hold_ratio_pct', label: '占总股本%', fmt: 'pct' },
		{ key: 'hold_change', label: '变动', fmt: 'text', title: '较上期增减股数, "不变"为文本' },
		{ key: 'shares_type', label: '股份类型', fmt: 'text' },
		{ key: 'market_cap', label: '持股市值', fmt: 'num', title: '单位元' },
	],
	fund_holding: [
		{ key: 'report_date', label: '报告期', fmt: 'date' },
		{ key: 'org_type', label: '机构类型', fmt: 'text', title: '基金/QFII/社保/保险/券商/信托/其他/机构汇总' },
		{ key: 'org_num', label: '机构家数', fmt: 'num' },
		{ key: 'hold_shares', label: '持股合计', fmt: 'num', title: '单位股' },
		{ key: 'hold_value', label: '持仓市值', fmt: 'num', title: '单位元' },
		{ key: 'free_ratio_pct', label: '占流通股%', fmt: 'pct' },
		{ key: 'hold_change', label: '变动', fmt: 'text', title: '较上期增仓/减仓/新进/不变' },
	],
	ipo_info: [
		{ key: 'apply_date', label: '申购日', fmt: 'date' },
		{ key: 'apply_code', label: '申购代码', fmt: 'text' },
		{ key: 'name', label: '名称', fmt: 'text' },
		{ key: 'listing_date', label: '上市日', fmt: 'date' },
		{ key: 'market', label: '板块', fmt: 'text' },
		{ key: 'issue_price', label: '发行价', fmt: 'dec', title: '元/股' },
		{ key: 'issue_num_wan', label: '发行量(万)', fmt: 'num', title: '单位万股' },
		{ key: 'raise_funds_yi', label: '募资(亿)', fmt: 'dec', title: '单位亿元' },
		{ key: 'pe_after_issue', label: '发行PE', fmt: 'dec' },
		{ key: 'industry_pe', label: '行业PE', fmt: 'dec' },
		{ key: 'win_rate_pct', label: '中签率%', fmt: 'pct', title: '网上发行中签率' },
		{ key: 'industry_name', label: '行业', fmt: 'text' },
	],
	perf_express: [
		{ key: 'report_date', label: '报告期', fmt: 'date' },
		{ key: 'data_type', label: '类型', fmt: 'text' },
		{ key: 'eps', label: 'EPS', fmt: 'dec', title: '基本每股收益, 元' },
		{ key: 'revenue', label: '营收', fmt: 'num', title: '营业总收入, 单位元' },
		{ key: 'revenue_yoy_pct', label: '营收同比%', fmt: 'spct' },
		{ key: 'net_profit', label: '净利润', fmt: 'num', title: '归母净利润, 单位元' },
		{ key: 'net_profit_yoy_pct', label: '净利同比%', fmt: 'spct' },
		{ key: 'bvps', label: '每股净资产', fmt: 'dec' },
		{ key: 'roe_pct', label: 'ROE%', fmt: 'pct', title: '加权平均净资产收益率' },
		{ key: 'notice_date', label: '公告日', fmt: 'date' },
	],
	// ---- P1-13 第三批 (新浪/腾讯源) ----
	money_flow: [
		{ key: 'date', label: '日期', fmt: 'date' },
		{ key: 'close', label: '收盘', fmt: 'dec' },
		{ key: 'chg_pct', label: '涨跌%', fmt: 'spct' },
		{ key: 'net_inflow', label: '净流入', fmt: 'num', title: '当日资金净流入, 单位元' },
		{ key: 'net_ratio_pct', label: '净占比%', fmt: 'spct', title: '净流入占成交额比例' },
		{ key: 'main_net', label: '主力净额', fmt: 'num', title: '超大单净额, 单位元(新浪口径主力=超大单)' },
		{ key: 'main_ratio_pct', label: '主力净占比%', fmt: 'spct' },
		{ key: 'industry_net', label: '行业净额', fmt: 'num', title: '所属行业资金净额, 单位元' },
		{ key: 'industry_chg_pct', label: '行业涨跌%', fmt: 'spct' },
	],
	tick_trade: [
		{ key: 'time', label: '时间', fmt: 'text' },
		{ key: 'price', label: '价格', fmt: 'dec' },
		{ key: 'change', label: '价差', fmt: 'dec', title: '与上一笔的价差, 单位元(不是百分比)' },
		{ key: 'volume_lots', label: '成交量(手)', fmt: 'num' },
		{ key: 'amount', label: '成交额', fmt: 'num', title: '单位元' },
		{ key: 'side', label: '方向', fmt: 'text', title: '买盘=主动买 / 卖盘=主动卖 / 中性=集合竞价等' },
	],
}

const TABS: { key: StockProfileDataset; label: string }[] = [
	{ key: 'unlock', label: '限售解禁' },
	{ key: 'pledge', label: '股权质押' },
	{ key: 'repurchase', label: '股票回购' },
	{ key: 'holder_number', label: '股东户数' },
	{ key: 'top_holders', label: '十大股东' },
	{ key: 'fund_holding', label: '基金持仓' },
	{ key: 'ipo_info', label: '新股申购' },
	{ key: 'perf_express', label: '业绩快报' },
	{ key: 'money_flow', label: '资金流向' },
	{ key: 'tick_trade', label: '逐笔成交' },
]

// 数据源标识 → 中文名(页脚展示, 与后端 DATASET_SOURCE 对应)
const SOURCE_LABELS: Record<string, string> = {
	eastmoney: '东方财富',
	sina: '新浪财经',
	tencent: '腾讯财经',
}

function fmtCell(v: unknown, fmt: Fmt): { text: string; cls?: string } {
	if (v == null || v === '') return { text: '—' }
	switch (fmt) {
		case 'date':
			return { text: String(v) }
		case 'num':
			return { text: typeof v === 'number' ? fmtBigNum(v) : String(v) }
		case 'dec':
			return { text: typeof v === 'number' ? v.toFixed(2) : String(v) }
		case 'pct':
			return { text: typeof v === 'number' ? `${v.toFixed(2)}%` : String(v) }
		case 'spct': {
			const n = typeof v === 'number' ? v : null
			return {
				text: n != null ? `${n > 0 ? '+' : ''}${n.toFixed(2)}%` : String(v),
				cls: n != null ? priceColorClass(n) : undefined,
			}
		}
		default:
			return { text: String(v) }
	}
}

export function StockProfilePanel({ symbol }: { symbol: string }) {
	const [dataset, setDataset] = useState<StockProfileDataset>('unlock')
	const qc = useQueryClient()

	const { data, isLoading, isFetching } = useQuery({
		queryKey: QK.stockProfile(dataset, symbol),
		queryFn: () => api.stockProfile(dataset, symbol),
		// 后端已有分级缓存, 前端再挡一层避免切 tab 反复请求:
		// 逐笔 30s(准实时) / 资金流 5min / 资料类 10min (P1-13 第三批)
		staleTime: dataset === 'tick_trade' ? 30_000 : dataset === 'money_flow' ? 5 * 60_000 : 10 * 60_000,
		refetchOnWindowFocus: false,
	})

	// 强制刷新: force=1 跳过后端缓存真拉一次, 结果直接写回查询缓存
	const refresh = useMutation({
		mutationFn: () => api.stockProfile(dataset, symbol, true),
		onSuccess: (d) => qc.setQueryData(QK.stockProfile(dataset, symbol), d),
	})

	const resp = data as StockProfileResponse | undefined
	const rows = (resp?.rows ?? []) as unknown as Record<string, unknown>[]
	const cols = COLUMNS[dataset]
	const unavailable = resp?.state === 'source_unavailable'

	return (
		<div className="rounded-card border border-border bg-surface overflow-hidden">
			{/* 头部: 标题 + 数据集切换 + 刷新 */}
			<div className="flex items-center justify-between gap-2 px-4 pt-3 pb-2 flex-wrap">
				<div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
					<Database className="h-3.5 w-3.5 text-accent" />
					个股资料
					<span className="text-[10px] text-muted font-normal">免费源 · 分级缓存</span>
				</div>
				<div className="flex items-center gap-1">
					{TABS.map(t => (
						<button
							key={t.key}
							onClick={() => setDataset(t.key)}
							className={`px-2 py-1 rounded-btn text-[11px] transition-colors ${
								dataset === t.key
									? 'bg-accent/15 text-accent border border-accent/30'
									: 'text-secondary hover:text-foreground border border-transparent hover:bg-elevated'
							}`}
						>
							{t.label}
						</button>
					))}
					<button
						onClick={() => refresh.mutate()}
						disabled={refresh.isPending || isFetching}
						className="ml-1 p-1.5 rounded-btn text-muted hover:text-accent hover:bg-elevated transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
						title="跳过缓存强制重拉"
					>
						{refresh.isPending
							? <Loader2 className="h-3 w-3 animate-spin" />
							: <RefreshCw className="h-3 w-3" />}
					</button>
				</div>
			</div>

			{/* 降级提示: 数据源不可用 / 过期缓存 */}
			{unavailable && (
				<div className="mx-4 mb-2 flex items-start gap-1.5 rounded-btn border border-warning/30 bg-warning/[0.06] px-2.5 py-1.5 text-[11px] text-warning">
					<AlertTriangle className="h-3 w-3 shrink-0 mt-0.5" />
					<span>{resp?.message || '资料数据源暂不可用'}</span>
				</div>
			)}
			{resp?.stale && (
				<div className="mx-4 mb-2 flex items-start gap-1.5 rounded-btn border border-warning/30 bg-warning/[0.06] px-2.5 py-1.5 text-[11px] text-warning">
					<AlertTriangle className="h-3 w-3 shrink-0 mt-0.5" />
					<span>{resp?.warning || '数据源暂不可用, 展示的是缓存数据'}</span>
				</div>
			)}

			{/* 主体: 表格 / 加载 / 空态 */}
			{isLoading ? (
				<div className="flex items-center justify-center py-10">
					<Loader2 className="h-4 w-4 animate-spin text-muted" />
				</div>
			) : rows.length === 0 ? (
				<div className="py-8 text-center text-xs text-muted">
					{unavailable ? '暂无数据' : `该标的暂无${TABS.find(t => t.key === dataset)?.label}记录`}
				</div>
			) : (
				<div className="max-h-72 overflow-auto px-4 pb-2">
					<table className="w-full text-xs">
						<thead className="sticky top-0 bg-surface">
							<tr className="border-b border-border/60">
								{cols.map(c => (
									<th
										key={c.key}
										title={c.title}
										className="py-1.5 px-2 text-left text-[10px] font-normal text-muted whitespace-nowrap"
									>
										{c.label}
									</th>
								))}
							</tr>
						</thead>
						<tbody>
							{rows.map((row, i) => (
								<tr key={i} className="border-b border-border/30 last:border-0 hover:bg-elevated/40">
									{cols.map(c => {
										const { text, cls } = fmtCell(row[c.key], c.fmt)
										// 长文本列(回购目的)截断 + 悬停看全文
										const long = c.fmt === 'text' && text.length > 18
										return (
											<td
												key={c.key}
												title={long ? text : c.title}
												className={`py-1.5 px-2 font-mono tabular-nums whitespace-nowrap ${cls ?? 'text-foreground'}`}
											>
												{long ? `${text.slice(0, 18)}…` : text}
											</td>
										)
									})}
								</tr>
							))}
						</tbody>
					</table>
				</div>
			)}

			{/* 页脚: 共 N 条 + 最近拉取时间 */}
			{resp?.fetched_at != null && (
				<div className="flex items-center gap-1 px-4 py-1.5 border-t border-border/40 text-[10px] text-muted">
					<Clock className="h-2.5 w-2.5" />
					共 {resp.count} 条 · 更新于 {new Date(resp.fetched_at * 1000).toLocaleString('zh-CN', {
						month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
					})}
					<span className="ml-auto">数据来源: {SOURCE_LABELS[resp.source] ?? resp.source}</span>
				</div>
			)}
		</div>
	)
}

// 行类型仅用于文档化契约(渲染按列定义走, 不做逐字段类型分发)
export type {
	ProfileUnlockRow, ProfilePledgeRow, ProfileRepurchaseRow, ProfileHolderNumberRow,
	ProfileTopHoldersRow, ProfileFundHoldingRow, ProfileIpoInfoRow, ProfilePerfExpressRow,
	ProfileMoneyFlowRow, ProfileTickTradeRow,
}
