import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
	CalendarDays,
	TrendingUp,
	FileText,
	Wallet,
	Activity,
	Sparkles,
	AlertTriangle,
	Loader2,
	ChartPie,
	Info,
	type LucideIcon
} from 'lucide-react'
import { type UseQueryResult } from '@tanstack/react-query'
import { useFinancialTable } from '@/lib/useFinancials'
import {
	requireFinancialSource,
	type FinancialFmt,
	type FinancialRow,
	type FinancialTab,
	type FinancialTableData,
	type FinancialTableMeta
} from '@/lib/financial-market-adapter'
import { type Market } from '@/lib/market'
import { fmtPrice, fmtBigNum, fmtDate } from '@/lib/format'
import { Skeleton } from '@/components/data/Skeleton'
import { startAnalysis, findLatestHistoryReport, openHistoryReport } from '@/lib/aiReportStore'
import { toast } from '@/components/Toast'

interface Props {
	symbol: string
	name: string
	/**
	 * 该标的所属市场, 由调用方(财务页)从全局市场开关传入。
	 *
	 * 🔴 刻意**不**在组件内用 `symbolMarket(symbol)` 推: tab 集 / 字段表 / 空态文案
	 * 都取决于市场, 而市场是**页面级**选择。用户刚切市场、还没选新标的时, 手里那个
	 * symbol 仍属旧市场 —— 按 symbol 推会用旧市场的字段表去读新市场的端点。
	 */
	market: Market
}

/** tab 图标是**表现层**, 与市场无关 ⇒ 留在组件里, 用全表 Record 保证不漏项。 */
const TAB_ICON: Record<FinancialTab, LucideIcon> = {
	metrics: TrendingUp,
	income: FileText,
	balance_sheet: Wallet,
	cash_flow: Activity,
	shares: ChartPie,
}

/**
 * 数值格式化。语义与搬进适配层之前逐字一致:
 * 🔴 `pct` 存的已经是百分点(12.3 表示 12.3%), **不再 ×100**; `amount` 走
 * `fmtBigNum`(万/亿/万亿, 不带币种符号 —— 币种由 `source.amountNote` 单独说明)。
 *
 * `int` 是港股新增的格式: 每手股数渲染成 `100.00` 是错的。
 * 入参收成 `unknown` + 统一转数: 港股列来自 parquet 投影, 出现字符串/`"--"` 时
 * 旧写法会在 `.toFixed` 上抛异常。
 */
function formatValue(raw: unknown, fmt: FinancialFmt): string {
	if (raw == null) return '—'
	const v = typeof raw === 'number' ? raw : Number(raw)
	if (!Number.isFinite(v)) return '—'
	switch (fmt) {
		case 'pct':
			return `${v.toFixed(2)}%`
		case 'amount':
			return fmtBigNum(v)
		case 'perShare':
			return fmtPrice(v, 2)
		case 'int':
			return String(Math.trunc(v))
		case 'num':
		default:
			return v.toFixed(2)
	}
}

export function StockFinancialDetail({ symbol, name, market }: Props) {
	const source = requireFinancialSource(market)
	const [tab, setTab] = useState<FinancialTab>('metrics')
	// AI 分析:点击时检查历史,若已有同标的报告则二次确认
	const [checking, setChecking] = useState(false)
	const [confirmReport, setConfirmReport] = useState<{ id: string; created_at: string; focus: string } | null>(null)

	const handleAiClick = async () => {
		if (checking) return
		setChecking(true)
		try {
			const latest = await findLatestHistoryReport(symbol)
			if (latest) {
				// 有历史报告 → 弹二次确认
				setConfirmReport({ id: latest.id, created_at: latest.created_at, focus: latest.focus })
			} else {
				// 无历史 → 直接分析
				await doAnalysis()
			}
		} catch {
			// 查询失败不阻塞,直接分析
			await doAnalysis()
		} finally {
			setChecking(false)
		}
	}

	const doAnalysis = async () => {
		const r = await startAnalysis(symbol, name)
		if (r.error) toast(r.error, 'error')
	}

	// 🔴 五个 hook **无条件全调**(hooks 数量必须恒定, 不能按 source.tabs 条件调用);
	// 该市场没有的表由适配层返回 enabled:false 的停用查询。港股四张表共用一个
	// query key + 各自 select ⇒ react-query 去重成一次请求。
	const metrics = useFinancialTable(market, 'metrics', symbol)
	const income = useFinancialTable(market, 'income', symbol)
	const balance = useFinancialTable(market, 'balance_sheet', symbol)
	const cashFlow = useFinancialTable(market, 'cash_flow', symbol)
	const shares = useFinancialTable(market, 'shares', symbol)

	const queryMap: Record<FinancialTab, UseQueryResult<FinancialTableData, Error>> = {
		metrics: metrics,
		income: income,
		balance_sheet: balance,
		cash_flow: cashFlow,
		shares: shares,
	}

	// 切市场后 tab 可能不在新市场的 tabs 里(港股无「股本」)⇒ 回落到第一个 tab。
	// 用派生值而不是 useEffect+setState: 后者会先渲染一帧「无按钮高亮 + 内容必空」。
	const activeTab: FinancialTab = source.tabs.includes(tab) ? tab : source.tabs[0]

	const current = queryMap[activeTab]
	// 按 period_end 降序(最新在前);同步默认 latest_only,通常只有1期
	const rows: FinancialRow[] = (current.data?.rows ?? []).slice().sort((a, b) =>
		(b.period_end ?? '').localeCompare(a.period_end ?? '')
	)
	const fieldDefs = source.fieldDefs(activeTab)
	// 标的级元信息(港股: 币种/机构类型); A 股恒 null。四张表同源, 兜底取 metrics。
	const meta: FinancialTableMeta | null = current.data?.meta ?? metrics.data?.meta ?? null
	const amountNote = source.amountNote(meta)
	const orgHint = source.orgTypeHint(meta)

	// 头部报告期信息取最新一期(优先用当前 tab,兜底用 metrics)
	const latestPeriod = rows[0]?.period_end ?? metrics.data?.rows?.[0]?.period_end ?? null
	const latestAnnounce = rows[0]?.announce_date ?? metrics.data?.rows?.[0]?.announce_date ?? null

	return (
		<div className="rounded-card border border-border bg-surface overflow-hidden">
			{/* 头部:标的 + 报告期 */}
			<div className="px-5 py-4 border-b border-border flex items-center gap-3 flex-wrap">
				<div className="flex items-baseline gap-2 min-w-0">
					<span className="text-lg font-semibold text-foreground">{name}</span>
					<span className="text-xs font-mono text-muted">{symbol}</span>
				</div>
				<div className="flex items-center gap-2 ml-auto">
					{source.capabilities.aiAnalysis && (
					<button
						onClick={handleAiClick}
						disabled={checking}
						className="inline-flex items-center gap-1 px-2.5 py-1 rounded-btn text-[11px] font-medium border border-purple-400/30 bg-purple-400/10 text-purple-300 hover:bg-purple-400/20 hover:border-purple-400/40 transition-all shrink-0 disabled:opacity-50"
						title="AI 财务分析"
					>
						{checking ? <Loader2 className="h-3 w-3 animate-spin" /> : <Sparkles className="h-3 w-3" />}
						AI 财务分析
					</button>
					)}
					{latestPeriod && (
						<div className="flex items-center gap-1.5 text-xs text-secondary">
							<CalendarDays className="h-3.5 w-3.5" />
							<span>报告期 <span className="font-mono">{latestPeriod}</span></span>
							{latestAnnounce && (
								<span className="text-muted">· 披露 {fmtDate(latestAnnounce)}</span>
							)}
						</div>
					)}
				</div>
			</div>

			{/* 标签页 —— 渲染哪些 tab 由适配层决定(港股无「股本」) */}
			<div className="flex items-center gap-1 px-3 pt-2 border-b border-border/60 overflow-x-auto">
				{source.tabs.map(key => {
					const Icon = TAB_ICON[key]
					const isActive = activeTab === key
					return (
						<button
							key={key}
							onClick={() => setTab(key)}
							className={`inline-flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 -mb-px transition-colors ${isActive
								? 'border-accent text-accent'
								: 'border-transparent text-muted hover:text-secondary'
								}`}
						>
							<Icon className="h-3.5 w-3.5" />
							{source.tabLabel(key)}
						</button>
					)
				})}
			</div>

			{/* 市场级口径说明: 币种 / 机构类型。A 股两者都为 null ⇒ 整块不渲染。 */}
			{(amountNote || orgHint) && (
				<div className="px-4 pt-3 space-y-1.5">
					{amountNote && (
						<div className="flex items-start gap-1.5 text-[11px] leading-relaxed text-amber-300/90 bg-amber-400/8 border border-amber-400/20 rounded-btn px-2.5 py-1.5">
							<Info className="h-3.5 w-3.5 shrink-0 mt-px" />
							<span>{amountNote}</span>
						</div>
					)}
					{orgHint && (
						<div className="text-[11px] leading-relaxed text-muted px-0.5">{orgHint}</div>
					)}
				</div>
			)}

			{/* 表格内容 */}
			<div className="p-4">
				{current.isLoading ? (
					<div className="space-y-2">
						{Array.from({ length: 6 }).map((_, i) => (
							<div key={i} className="flex justify-between">
								<Skeleton w="w-32" h="h-4" />
								<Skeleton w="w-20" h="h-4" />
							</div>
						))}
					</div>
				) : rows.length === 0 ? (
					<div className="py-10 px-6 text-center text-xs text-muted leading-relaxed">
						{source.tableEmptyHint(activeTab, symbol)}
					</div>
				) : (
					<div className="space-y-5">
						{/* 多期时为每期渲染一组;单期时只有一组 */}
						{rows.map((row, ri) => (
							<div key={row.period_end ?? ri}>
								{rows.length > 1 && (
									<div className="text-[11px] text-muted mb-2 flex items-center gap-1.5">
										<CalendarDays className="h-3 w-3" />
										报告期 <span className="font-mono text-secondary">{row.period_end}</span>
										{/* 港股: 年报/中报/一季报/三季报。财年结日不固定在年末
										    (阿里 6 月底是「一季报」), 光看日期会误判 ⇒ 必须标出来。 */}
										{source.periodBadge(row) && (
											<span className="px-1.5 py-px rounded-btn bg-elevated text-secondary">
												{source.periodBadge(row)}
											</span>
										)}
									</div>
								)}
								<div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-0">
									{fieldDefs.map(def => {
										const val = row[def.key]
										return (
											<div
												key={def.key}
												className="flex items-baseline justify-between gap-3 py-2 border-b border-border/40"
											>
												<span className="text-xs text-secondary shrink-0">{def.label}</span>
												<span className="text-sm font-mono tabular-nums text-foreground text-right">
													{formatValue(val, def.fmt)}
												</span>
											</div>
										)
									})}
								</div>
							</div>
						))}
					</div>
				)}
			</div>

			{/* AI 分析二次确认:已有该标的历史报告 */}
			<AnimatePresence>
				{confirmReport && (
					<div className="fixed inset-0 z-[70] flex items-center justify-center">
						<motion.div
							initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
							transition={{ duration: 0.15 }}
							className="absolute inset-0 bg-black/50 backdrop-blur-sm"
							onClick={() => setConfirmReport(null)}
						/>
						<motion.div
							initial={{ opacity: 0, scale: 0.95, y: 12 }}
							animate={{ opacity: 1, scale: 1, y: 0 }}
							exit={{ opacity: 0, scale: 0.97, y: 8 }}
							transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
							className="relative w-[90vw] max-w-[400px] rounded-card border border-border bg-base shadow-2xl p-6"
						>
							<div className="flex items-start gap-3">
								<div className="shrink-0 h-10 w-10 rounded-full bg-purple-400/12 flex items-center justify-center">
									<AlertTriangle className="h-5 w-5 text-purple-300" />
								</div>
								<div className="min-w-0 flex-1">
									<h3 className="text-sm font-semibold text-foreground mb-1.5">该个股已有分析报告</h3>
									<p className="text-xs text-secondary leading-relaxed">
										<span className="font-medium text-foreground">{name}</span>
										<span className="font-mono text-muted"> {symbol}</span> 在
										<span className="text-purple-300 font-medium"> {fmtReportTime(confirmReport.created_at)} </span>
										已生成过 AI 财务分析报告。
									</p>
									<p className="mt-2 text-[11px] text-muted">
										您可以查看历史报告,或基于最新数据重新生成一份。
									</p>
								</div>
							</div>
							<div className="flex items-center justify-end gap-2 mt-5">
								<button
									onClick={() => setConfirmReport(null)}
									className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-xs transition-colors"
								>
									取消
								</button>
								<button
									onClick={() => {
										if (confirmReport) openHistoryReport(confirmReport.id);
										setConfirmReport(null)
									}}
									className="px-3 py-1.5 rounded-btn border border-border text-secondary hover:text-foreground text-xs font-medium transition-colors"
								>
									查看历史报告
								</button>
								<button
									onClick={() => {
										doAnalysis();
										setConfirmReport(null)
									}}
									className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-gradient-to-r from-purple-500/80 to-fuchsia-500/80 text-white text-xs font-medium hover:from-purple-500 hover:to-fuchsia-500 transition-all"
								>
									<Sparkles className="h-3.5 w-3.5" />
									重新分析
								</button>
							</div>
						</motion.div>
					</div>
				)}
			</AnimatePresence>
		</div>
	)
}

// 历史报告时间友好显示
function fmtReportTime(iso: string): string {
	try {
		const t = new Date(iso).getTime()
		const diff = Date.now() - t
		if (diff < 60_000) return '刚刚'
		if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
		if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`
		if (diff < 7 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`
		return new Date(iso).toLocaleString('zh-CN', {
			month: '2-digit',
			day: '2-digit',
			hour: '2-digit',
			minute: '2-digit'
		})
	} catch {
		return ''
	}
}
