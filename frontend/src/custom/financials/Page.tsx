import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
	RefreshCw,
	Download,
	Lock,
	Loader2,
	X,
	Search,
	FileText,
	Database,
	Clock,
	CheckCircle2,
	Hourglass,
	Lightbulb,
	ExternalLink,
	ChartPie,
	Info
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { useCapabilities, useCapabilityMatrix } from '@/lib/useSharedQueries'
import { routeCapUsable } from '@/lib/capability-labels'
import { useFinancialStatusView, useFinancialTableSync } from '@/lib/useFinancials'
import { requireFinancialSource } from '@/lib/financial-market-adapter'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useMarket } from '@/lib/marketStore'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import { StockFinancialDetail } from '@/components/financials/StockFinancialDetail'
import { ReportHistoryPanel } from '@/components/financials/ReportHistoryPanel'
import { LastStockChip } from '@/components/LastStockChip'
import { useLastStock } from '@/lib/useLastStock'
import { fmtBigNum } from '@/lib/format'
import { toast } from '@/components/Toast'

/**
 * 状态卡片图标 —— 表现层, 按**卡片 key** 查(跨市场并集), 查不到兜底 Database。
 *
 * 🔴 刻意不用 `Record<FinancialTab, …>` 强制全表: 港股卡片 key 是**存储表名**
 * (`main_indicator` / `income` / `balance`), 与 tab key 不是一一对应(四张规范表
 * 都由主指标宽表投影)。用带兜底的普通 Record, 新增市场的卡片不必回来改这里。
 */
const CARD_ICON: Record<string, typeof FileText> = {
	metrics: Database,
	income: FileText,
	balance_sheet: FileText,
	cash_flow: FileText,
	shares: ChartPie,
	main_indicator: Database,
	balance: FileText,
}

export function Financials() {
	const market = useMarket()
	const source = requireFinancialSource(market)
	const qc = useQueryClient()
	const { data: caps } = useCapabilities()
	// 🔴 能力矩阵必须按市场取: 后端 `financial.CN` 与 `financial.HK` 是两条独立记录
	// (港股 source=eastmoney)。不传 market 会拿 A 股那条去门控港股页面。
	const { data: matrix } = useCapabilityMatrix(market)
	const { data: statusView, isLoading } = useFinancialStatusView(market)
	// 路由感知门控: 生效源当前能否提供财务数据 (含插件/自定义源);
	// 矩阵未加载时回退 TickFlow 套餐视角 + 后端可用状态
	const hasFinancial = routeCapUsable(matrix, 'financial')
		?? (caps?.capabilities?.['financial'] != null || statusView?.available === true)
	const syncMut = useFinancialTableSync(market)

	// ── 整批任务制市场(港股)的同步进度 ─────────────────────────────
	// A 股: 同步请求阻塞在 HTTP 里, 进度靠 status.syncing + 每表 last_sync 推算。
	// 港股: `POST /api/hk/sync_financials` 立刻返回 job_id(与日K同步共用重任务槽),
	// 进度住在 job store ⇒ 必须轮询 `pipelineJob(id)`。
	// 🔴 用 capabilities.jobSync 分流而不是 `if (market === 'HK')`
	// (CONTRIBUTING §3.5: 市场差异是数据不是控制流)。
	const [jobId, setJobId] = useState<string | null>(null)
	const job = useQuery({
		queryKey: QK.pipelineJob(jobId ?? ''),
		queryFn: () => api.pipelineJob(jobId!),
		enabled: source.capabilities.jobSync && !!jobId,
		refetchInterval: (q: any) => {
			const st = q.state.data?.status
			return st === 'succeeded' || st === 'failed' ? false : 1_000
		},
	})
	const jobStatus = job.data?.status
	const jobRunning = source.capabilities.jobSync && !!jobId
		&& jobStatus !== 'succeeded' && jobStatus !== 'failed'

	// job 收尾: 失效本市场全部财务缓存(状态卡 + 已打开的详情)并停止轮询。
	// 🔴 只失效 `source.invalidateKeys`(港股 = ['hk-financials']), 不广域失效:
	// A 股财务缓存与它无关, 全量 invalidate 会让另一个市场的页面白白重取。
	useEffect(() => {
		if (!jobId || (jobStatus !== 'succeeded' && jobStatus !== 'failed')) return
		source.invalidateKeys.forEach(queryKey => qc.invalidateQueries({ queryKey }))
		if (jobStatus === 'failed') toast('财务同步任务失败，详见「数据」页任务日志', 'error')
		setJobId(null)
	}, [jobId, jobStatus, qc, source])

	// 同步进行中 = 服务端真值(status.syncing) / job 在跑 / 本地乐观态(请求已发出待确认)。
	// 乐观窗口:点击后到 invalidate 触发的 refetch 返回之间,status.syncing 暂为 false,
	// 用 syncMut.isPending 覆盖,让按钮立即置灰、避免重复点击。
	// 后端 trigger() 返回时 syncing 已为 true,refetch 到达后 status.syncing 接管。
	const syncing = (statusView?.syncing ?? false) || jobRunning || syncMut.isPending
	// 本次同步开始时间戳(ms): 用于判断每张表的 last_sync 是否属于本次同步
	// (后端每张表完成即更新 last_sync, 前端轮询时对比时间戳得到精确进度)
	const [syncStartedAt, setSyncStartedAt] = useState<number | null>(null)
	// 单表同步时记录表名 (null = 全量同步), 用于区分卡片状态
	const [syncSingleTable, setSyncSingleTable] = useState<string | null>(null)
	// 同步自然结束(服务端 syncing 由 true→false):清空本次同步记录。
	// 这是可靠的收尾时机 —— 不依赖 mutation 的 onSettled(它现在瞬间触发,会误清)。
	useEffect(() => {
		if (!syncing && syncStartedAt !== null) {
			setSyncStartedAt(null)
			setSyncSingleTable(null)
		}
	}, [syncing, syncStartedAt])
	// 选中的个股(模糊搜索结果);null 时显示搜索引导
	const [selected, setSelected] = useState<{ symbol: string; name: string } | null>(null)
	const { last: lastStock, remember: rememberStock } = useLastStock('financials')

	// 🔴 切市场必须清掉已选标的: 港股端点对 A 股代码直接 400(«非港股代码格式»),
	// 留着会渲染一个必然报错的详情面板。job / 单表进度记录同理(都属于旧市场)。
	useEffect(() => {
		setSelected(null)
		setJobId(null)
		setSyncStartedAt(null)
		setSyncSingleTable(null)
	}, [market])

	const pick = (symbol: string, name: string) => {
		setSelected({ symbol, name })
		rememberStock(symbol, name)
	}
	// 「上次查看」跨市场**不**恢复: localStorage 里那只可能属于另一个市场,
	// 点一下就会拿旧市场代码打新市场端点。
	const restorableStock = lastStock && source.ownsSymbol(lastStock.symbol) ? lastStock : null

	if (!hasFinancial) {
		return (
			<>
				<PageHeader title="财务概况" subtitle={source.pageSubtitle} />
				<div className="px-8 py-10">
					<div className="mx-auto max-w-md rounded-card border border-warning/30 bg-warning/[0.04] p-8 text-center">
						<div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-warning/10">
							<Lock className="h-6 w-6 text-warning" />
						</div>
						<h3 className="mt-4 text-[16px] leading-6 font-semibold text-foreground">财务数据不可用</h3>
						<p className="mt-2 text-xs leading-relaxed text-secondary">
							当前数据源未提供财务数据。配置提供财务数据的数据源后,此页自动显示财务数据面板。
						</p>
						<Link
							to="/settings?tab=data-sources"
							className="mt-4 inline-flex items-center gap-1.5 rounded-btn bg-accent/90 px-3.5 py-1.5 text-xs font-medium text-white hover:bg-accent transition-colors"
						>
							前往数据源配置
						</Link>
						{/* 当前财务数据源(TickFlow)需付费,后续将接入免费数据源;期间欢迎在 issues 推荐免费源 */}
						<div className="mt-5 rounded-btn border border-accent/25 bg-accent/[0.05] px-3.5 py-3 text-left">
							<div className="flex items-center gap-1.5 text-xs font-medium text-accent">
								<Lightbulb className="h-3.5 w-3.5 shrink-0" />
								关于数据源
							</div>
							<p className="mt-1.5 text-[11px] leading-relaxed text-secondary">
								当前财务数据源需付费,后续会接入免费数据源。如你常用某个免费财务数据源,欢迎在 Issues 中多多推荐哈 ~
							</p>
							<a
								href="https://github.com/shy3130/tough-leek-quant/issues"
								target="_blank"
								rel="noreferrer"
								className="mt-2 inline-flex items-center gap-1 text-[11px] font-medium text-accent hover:underline"
							>
								前往 Issues 推荐
								<ExternalLink className="h-3 w-3" />
							</a>
						</div>
					</div>
				</div>
			</>
		)
	}

	const handleSync = (table: string) => {
		// 防重复点击:syncing 中不再触发(后端 trigger 也有 _is_syncing 兜底)
		if (syncing) return
		if (source.capabilities.jobSync) {
			// 整批任务制: 后端立刻回 job_id, 之后靠轮询。没有「第几张表」的概念,
			// 所以**不设** syncStartedAt —— 那套推算依赖每表 last_sync, 港股没有。
			syncMut.mutate(table, {
				onSuccess: (r) => {
					if (r.jobId) setJobId(r.jobId)
					else toast('同步未能开始:后端未返回任务号', 'error')
				},
			})
			return
		}
		// 记录开始时间: 全量同步判断所有财务表, 单表同步只判断这一张
		setSyncStartedAt(Date.now())
		setSyncSingleTable(table === 'all' ? null : table)
		syncMut.mutate(table, {
			onSuccess: (r) => {
				// 后端 trigger 立即返回 started 状态;若被防并发跳过(已有同步在进行),
				// 给用户明确反馈,并清空本次误设的记录。
				if (!r.started) {
					if (r.reason === 'already running') {
						toast('财务数据正在同步中,请稍候', 'success')
					} else if (r.reason === 'no FINANCIAL capability') {
						// 能力未就绪:通常发生在升级/刷新 Key 后调度器状态未同步 —— 提示用户检查 Key
						toast('财务数据能力未就绪,请检查 API Key 或刷新页面后重试', 'error')
					} else {
						toast(`同步未能开始${r.reason ? `:${r.reason}` : ''}`, 'error')
					}
					setSyncStartedAt(null)
					setSyncSingleTable(null)
				}
			},
			onError: () => {
				// 请求失败:清空本次记录(request 已弹错误 toast)
				setSyncStartedAt(null)
				setSyncSingleTable(null)
			},
		})
	}

	const cards = statusView?.cards ?? []
	const cardByKey = new Map(cards.map(c => [c.key, c]))
	const available = statusView?.available ?? false
	const notes = statusView?.notes ?? []
	// 参与「第几张表完成」推算的表序。
	// 🔴 整批任务制市场为空数组 ⇒ 下面几个推算自然全部落空(找不到 currentSyncingTable、
	// syncedCount=0), 进度改由 jobRunning / job.progress 提供 —— 不需要额外分支。
	const syncOrder = statusView?.syncOrder ?? []
	// 本次同步进度: 仅当 syncStartedAt 存在且 syncing 时, 按 last_sync 时间戳判断
	const isFullSync = syncing && syncStartedAt && !syncSingleTable  // 全量同步
	const isSingleSync = syncing && syncStartedAt && !!syncSingleTable  // 单表同步
	const tableDoneThisRound = (key: string): boolean => {
		if (!syncStartedAt || !syncing) return false
		// 单表同步: 只判断这一张表是否完成
		if (syncSingleTable && key !== syncSingleTable) return false
		const ls = cardByKey.get(key)?.lastSync
		if (!ls) return false
		return new Date(ls).getTime() >= syncStartedAt
	}
	// 当前正在同步的表:
	// 全量同步 → 第一个未完成的; 单表同步 → 那张表(未完成时)
	const currentSyncingTable = syncing && syncStartedAt
		? (syncSingleTable
			? (tableDoneThisRound(syncSingleTable) ? null : syncSingleTable)
			: syncOrder.find(t => !tableDoneThisRound(t)) ?? null)
		: null
	const syncedCount = syncOrder.filter(t => tableDoneThisRound(t)).length
	// 卡片三态: 仅全量同步时未轮到的表显示"等待"; 单表同步时其他表保持原样
	const isWaitingTable = (key: string): boolean =>
		!!isFullSync && !tableDoneThisRound(key) && currentSyncingTable !== key
	const progressLabel = isFullSync
		? `已同步 ${syncedCount}/${syncOrder.length} 张表…`
		: isSingleSync
			? `同步${cardByKey.get(syncSingleTable!)?.label ?? syncSingleTable}…`
			: jobRunning
				? `同步中 ${Math.round(job.data?.progress ?? 0)}%…`
				: '同步中…'

	return (
		<>
			<PageHeader
				title="财务概况"
				subtitle={source.pageSubtitle}
				right={
					<div className="flex items-center gap-2">
						<LastStockChip stock={restorableStock} onSelect={pick} />
						{syncing && (
							<span className="text-xs text-accent/80 flex items-center gap-1.5">
								<Loader2 className="w-3 h-3 animate-spin" />
								{progressLabel}
							</span>
						)}
						<button
							className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-gradient-to-r from-accent/25 to-accent/10 border border-accent/30 text-accent text-xs font-medium hover:from-accent/35 hover:to-accent/20 transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed"
							onClick={() => handleSync('all')}
							disabled={syncing}
							title={syncing ? '正在同步，请稍候…' : `同步全部${source.content.label}财务表`}
						>
							{syncing
								? <Loader2 className="h-3.5 w-3.5 animate-spin" />
								: <RefreshCw className="h-3.5 w-3.5" />}
							{syncing ? '同步中…' : source.syncAllLabel}
						</button>
					</div>
				}
			/>

			<div className="px-3 sm:px-8 py-6 space-y-6 max-w-7xl">
				{/* 市场级说明(港股: 分开存储 / 无股本表)。A 股为空串 ⇒ 不渲染。 */}
				{source.headerHint && (
					<div className="flex items-start gap-2 rounded-card border border-border/60 bg-elevated/25 px-3 py-2 text-[11px] leading-relaxed text-secondary">
						<Info className="h-3.5 w-3.5 shrink-0 mt-px text-muted" />
						<span>{source.headerHint}</span>
					</div>
				)}

				{syncing && (
					<div className="flex items-center gap-2 rounded-card border border-accent/30 bg-accent/[0.06] px-3 py-2 text-xs text-accent">
						<Loader2 className="h-3.5 w-3.5 animate-spin shrink-0" />
						正在从财务数据源拉取数据，请稍候…
					</div>
				)}

				{/* 同步状态卡片 —— 始终显示,反映本地财务数据概况 */}
				{!isLoading && available && (
					<div>
						<div className={`grid ${source.cardGridClass} gap-3`}>
							{cards.map(card => {
								const TIcon = CARD_ICON[card.key] ?? Database
								const hasData = card.rows > 0
								// 本次同步三态: 完成 / 同步中 / 等待 (仅全量同步时未轮到的表才"等待")
								const doneThisRound = tableDoneThisRound(card.key)
								const isThisSyncing = currentSyncingTable === card.key
								const isWaiting = isWaitingTable(card.key)
								return (
									<div
										key={card.key}
										className={`rounded-card border p-3.5 transition-colors flex flex-col ${isThisSyncing
											? 'border-accent/40 bg-accent/[0.04]'
											: isWaiting
												? 'border-border/50 bg-elevated/15'
												: hasData
													? 'border-border bg-surface'
													: 'border-dashed border-border/60 bg-elevated/20'
											}`}
									>
										<div className="flex items-center justify-between">
											<div className="flex items-center gap-1.5">
												{doneThisRound ? (
													<CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
												) : isThisSyncing ? (
													<Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
												) : isWaiting ? (
													<Hourglass className="h-3.5 w-3.5 text-muted/60" />
												) : (
													<TIcon className={`h-3.5 w-3.5 ${hasData ? 'text-accent' : 'text-muted'}`} />
												)}
												<span className="text-xs font-medium text-foreground">{card.label}</span>
											</div>
											{/* 🔴 能力位门控: 整批任务制市场没有每表同步入口,
											    渲染一个必然无效的按钮比不渲染更糟(方案 §8 规则 4)。 */}
											{source.capabilities.perTableSync && (
												<button
													className="text-muted hover:text-accent transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
													onClick={() => handleSync(card.key)}
													disabled={syncing}
													title={syncing ? '正在同步…' : `更新${card.label}`}
												>
													{syncing
														? <Loader2 className="h-3.5 w-3.5 animate-spin" />
														: <Download className="h-3.5 w-3.5" />}
												</button>
											)}
										</div>
										<div className="mt-2 text-xl font-semibold tabular-nums text-foreground">
											{fmtBigNum(card.rows)}
											<span className="text-[10px] text-muted ml-1 font-normal">行</span>
										</div>
										<div className="text-[11px] text-muted mt-0.5">
											{/* null = 该表不提供标的数口径 ⇒ 渲染「—」而不是 0
											    (0 会被读成「同步过但一只都没有」)。 */}
											{card.symbols == null ? '按标的同步' : `${fmtBigNum(card.symbols)} 只标的`}
										</div>
										<div className="mt-auto pt-2 border-t border-border/40 text-[10px] text-muted flex items-center gap-1">
											<Clock className="h-2.5 w-2.5 shrink-0" />
											{card.lastSync
												? new Date(card.lastSync).toLocaleString('zh-CN', {
													month: '2-digit',
													day: '2-digit',
													hour: '2-digit',
													minute: '2-digit'
												})
												// 🔴 无 last_sync 有两种完全不同的含义, 用能力位区分:
												// 每表可同步的市场 = 这张表还没同步过; 整批任务制市场
												// = 后端根本不记每表时间(说「尚未同步」会是假话)。
												: source.capabilities.perTableSync ? '尚未同步' : '整批任务，无单表时间'}
										</div>
									</div>
								)
							})}
						</div>
						{/* 市场级摘要(港股: 最新报告期 / 每手股数覆盖 / 报告期类型)。 */}
						{notes.length > 0 && (
							<div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
								{notes.map(n => <span key={n}>{n}</span>)}
							</div>
						)}
					</div>
				)}

				{isLoading ? (
					<div className="flex items-center justify-center py-16">
						<Loader2 className="h-5 w-5 animate-spin text-muted" />
					</div>
				) : !available ? (
					<div className="rounded-card border border-dashed border-border bg-surface px-6 py-14 text-center">
						<Database className="mx-auto h-8 w-8 text-muted" />
						<div className="mt-3 text-sm text-secondary">暂无{source.content.label}财务数据</div>
						<div className="mt-1 text-xs text-muted">点击右上角「{source.syncAllLabel}」从当前数据源拉取</div>
					</div>
				) : (
					<>
						{/* 个股搜索区 */}
						<div>
							{selected ? (
								// 已选个股:紧凑搜索条 + 清除按钮(便于换股)
								<div className="flex items-center gap-3">
									<div className="flex-1 max-w-xl">
										<StockFinancialSearch
											onSelect={pick}
											market={market}
											placeholder={source.searchPlaceholder}
										/>
									</div>
									<button
										onClick={() => setSelected(null)}
										className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs text-secondary hover:text-foreground rounded-btn border border-border hover:bg-elevated transition-colors shrink-0"
										title="清除选择"
									>
										<X className="h-3.5 w-3.5" />
										清除
									</button>
								</div>
							) : (
								// 未选个股:醒目居中引导
								<div className="flex flex-col items-center gap-3 py-8">
									<div className="flex items-center gap-2 text-sm text-secondary">
										<Search className="h-4 w-4 text-accent" />
										<span>搜索标的查看详细财务数据</span>
									</div>
									<div className="w-full max-w-xl">
										<StockFinancialSearch
											onSelect={pick}
											market={market}
											placeholder={source.searchPlaceholder}
										/>
									</div>
									<div className="text-[11px] text-muted">{source.searchExample}</div>
								</div>
							)}
						</div>

						{/* 个股详情 / 空引导 */}
						<div className="pb-4">
							{selected ? (
								<StockFinancialDetail
									symbol={selected.symbol}
									name={selected.name}
									market={market}
								/>
							) : (
								<EmptyState
									icon={Search}
									title="未选择标的"
									hint={`在上方搜索框输入${source.content.label}代码或名称，选择后即可查看该标的的${source.tabs.map(t => source.tabLabel(t)).join('、')}。`}
								/>
							)}
						</div>

						{/* AI 历史分析报告(跨市场共用一份历史, 不按市场过滤) */}
						{available && <ReportHistoryPanel />}
					</>
				)}
			</div>
		</>
	)
}
