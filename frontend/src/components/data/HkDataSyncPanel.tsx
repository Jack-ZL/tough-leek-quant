/**
 * 港股数据 · 同步面板 (数据页「港股数据」画像卡的弹窗内容)。
 *
 * 港股不在 A 股盘后管道里 (独立端点 + 独立存储分区), 顶部「立即同步」不含它 ——
 * 此前想同步港股只能靠定时流水线或直接调 API。本面板把画像与三个同步入口
 * (维表 / 日K+指标 / 重算指标) 收进画像卡, 与其它数据集的交互口径一致。
 *
 * 🔴 三处刻意不复用 A 股实现:
 * 1. **进度不抄 FinancialSyncPanel 的 `last_sync` 时间戳比对** —— 港股同步没有
 *    每表同步时间, 进度只能来自 job 的 `stage_pct` (由 Data.tsx 的 activeJobId 驱动)。
 * 2. **`sync_instruments` 的 `status:'empty'` 不当成功** —— 该值同时表示「抓取失败
 *    (保留旧维表、一行没写)」与「上游真的返回空」, 报成功会让用户以为维表是新的。
 * 3. **复权凭据缺失/失效只解释、不给绕过按钮** —— `sync_hk_adj_factor` 没有 HTTP
 *    入口, 唯一正解是等下一轮定时流水线; 给个「强制重算」等于教用户拆 fail-closed 闸门。
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, CheckCircle2, AlertTriangle, CircleDashed, RefreshCw, Table2 } from 'lucide-react'
import { api, type HkDataStatus } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

type Props = {
	status: HkDataStatus | undefined
	isLoading: boolean
	/** 有任意数据任务在跑 (与 A 股共用重任务槽, 撞车会被后端拒绝) */
	isRunning: boolean
	/** 异步 job 受理后交给 Data.tsx 轮询进度 */
	onStart: (jobId: string) => void
}

type Row = {
	key: string
	label: string
	/** null = 该块从未同步 (后端返回 null 而非零值) */
	present: boolean
	main: string
	sub: string
	/** 右侧补充 (通常是日期区间) */
	note?: string
}

function range(a: string | null | undefined, b: string | null | undefined): string {
	if (!a && !b) return '—'
	return `${a ?? '?'} ~ ${b ?? '?'}`
}

export function HkDataSyncPanel({ status, isLoading, isRunning, onStart }: Props) {
	const qc = useQueryClient()

	// 维表同步是**同步执行**的秒级请求, 不产生 job ⇒ 自己管 pending 与失效。
	const syncDim = useMutation({
		mutationFn: () => api.hkSyncInstruments(),
		onSuccess: (r) => {
			if (r.status === 'empty') {
				// 🔴 歧义值: 抓取失败与上游真空同报 empty, 且失败时旧维表原样保留。
				// 报「同步完成」会让用户以为维表是新的 → 后续日K同步按旧universe跑。
				toast('没拿到港股维表数据 (抓取失败或上游返回空); 原有维表已保留, 请查后端日志', 'error')
			} else {
				toast(`港股维表已同步: ${r.count} 条`, 'success')
			}
			qc.invalidateQueries({ queryKey: QK.hkStatus })
			qc.invalidateQueries({ queryKey: ['hk-instruments'] })
		},
	})

	const syncDaily = useMutation({
		mutationFn: () => api.hkSyncDaily(),
		onSuccess: ({ job_id }) => {
			onStart(job_id)
			qc.invalidateQueries({ queryKey: QK.pipelineJobs })
		},
		// 409 (维表为空) 不额外处理: request() 已把后端 detail 弹成 toast,
		// 且按钮在维表缺失时本就禁用 —— 再叠一条提示只是噪音。
	})

	const syncEnriched = useMutation({
		mutationFn: () => api.hkSyncEnriched(),
		onSuccess: ({ job_id }) => {
			onStart(job_id)
			qc.invalidateQueries({ queryKey: QK.pipelineJobs })
		},
	})

	if (isLoading) {
		return (
			<div className="flex items-center justify-center gap-2 py-10 text-xs text-muted">
				<Loader2 className="h-4 w-4 animate-spin" />读取港股数据画像…
			</div>
		)
	}

	if (!status) {
		return (
			<div className="rounded-btn border border-dashed border-border px-4 py-6 text-center text-[11px] text-muted">
				港股数据画像读取失败, 请稍后重试或检查后端服务。
			</div>
		)
	}

	const { instruments, daily, enriched, index_daily, index_enriched, adj_factor, financials, storage } = status

	const rows: Row[] = [
		{
			key: 'instruments',
			label: '个股维表',
			present: !!instruments,
			// 🔴 双柜台分开报: 人民币柜台与港币柜台是同一份基本面的两个交易代码,
			// 混成一个「标的数」会让人以为港股有 5000+ 只独立公司。
			main: instruments ? `${instruments.hkd_counters.toLocaleString()} 只港币柜台` : '—',
			sub: instruments
				? `另有 ${instruments.cny_counters.toLocaleString()} 只人民币柜台 · ${instruments.industries.toLocaleString()} 个行业`
				: '未同步',
			note: instruments?.latest_as_of ? `快照 ${instruments.latest_as_of}` : undefined,
		},
		{
			key: 'daily',
			label: '日 K',
			present: !!daily,
			// 🔴 个股日K的 `rows` 是「没统计」哨兵 (逐日分区全量 count 太贵), 判空看 trading_days。
			main: daily ? `${daily.trading_days.toLocaleString()} 个交易日` : '—',
			sub: daily ? `${daily.symbols_covered.toLocaleString()} 只标的 (最新分区)` : '未同步',
			note: daily ? range(daily.earliest_date, daily.latest_date) : undefined,
		},
		{
			key: 'enriched',
			label: 'Enriched',
			present: !!enriched,
			main: enriched ? `${enriched.fields ?? 0} 列指标` : '—',
			sub: enriched
				? `${enriched.trading_days.toLocaleString()} 日 · ${enriched.symbols_covered.toLocaleString()} 只标的`
				: '未同步',
			note: enriched ? range(enriched.earliest_date, enriched.latest_date) : undefined,
		},
		{
			key: 'index_daily',
			label: '指数日 K',
			present: !!index_daily,
			// 指数表数据量小 (数十只 × 数百日), 后端给的是**精确行数**, 不是哨兵。
			main: index_daily ? `${index_daily.rows.toLocaleString()} 行` : '—',
			sub: index_daily
				? `${index_daily.symbols_covered.toLocaleString()} 只指数 · ${index_daily.trading_days.toLocaleString()} 日`
				: '未同步',
			note: index_daily ? range(index_daily.earliest_date, index_daily.latest_date) : undefined,
		},
		{
			key: 'index_enriched',
			label: '指数 Enriched',
			present: !!index_enriched,
			main: index_enriched ? `${index_enriched.rows.toLocaleString()} 行` : '—',
			sub: index_enriched ? `${index_enriched.fields ?? 0} 列指标` : '未同步',
			note: index_enriched ? range(index_enriched.earliest_date, index_enriched.latest_date) : undefined,
		},
		{
			key: 'adj_factor',
			label: '复权因子',
			present: !!adj_factor,
			main: adj_factor ? `${adj_factor.covered.toLocaleString()} 只已覆盖` : '—',
			sub: adj_factor
				? adj_factor.uncovered > 0
					? `${adj_factor.uncovered.toLocaleString()} 只未覆盖`
					: `${adj_factor.rows.toLocaleString()} 条事件 · 覆盖完整`
				: '未同步',
			note: adj_factor?.coverage_updated_at ? `凭据 ${adj_factor.coverage_updated_at.slice(0, 10)}` : undefined,
		},
		{
			key: 'financials',
			label: '财务数据',
			// 财务块永不为 null (未同步时 synced:false), 用 synced 判存在
			present: financials.synced,
			main: financials.synced ? `${financials.rows.toLocaleString()} 行` : '—',
			sub: financials.synced
				? `${financials.symbols.toLocaleString()} 只标的 · 每手股数已回填 ${financials.lot_size_coverage ?? 0} 只`
				: '未同步',
			note: financials.latest_report_date ? `最新报告期 ${financials.latest_report_date}` : undefined,
		},
	]

	// ── 告警: 只解释原因与正解, 不提供绕过 fail-closed 闸门的按钮 ──────────
	const warnings: string[] = []
	if (!instruments) {
		warnings.push('港股维表为空 —— 日K同步会被后端拒绝 (409)。请先点下方「同步维表」。')
	}
	if (!adj_factor) {
		warnings.push(
			'复权因子从未同步: 完整性凭据 adj_factor_hk/coverage.json 不存在, 港股 Enriched 会整轮零行 (fail-closed)。'
			+ ' sync_hk_adj_factor 目前没有独立 HTTP 入口, 只由港股定时流水线调用 —— 请等下一轮定时同步补齐后再重算指标。',
		)
	} else if (!adj_factor.has_coverage) {
		warnings.push(
			adj_factor.coverage_unreadable
				? '复权凭据文件存在但读不出来 (schema 版本不符或 JSON 损坏) —— 与「没跑过」不同, 这通常意味着版本迁移没跟上, 请查后端日志里的 load_hk_adj_coverage 报错。'
				: '复权凭据无效, 港股 Enriched 会整轮零行。请等下一轮港股定时流水线补齐复权因子。',
		)
	} else if (adj_factor.uncovered > 0) {
		warnings.push(
			`${adj_factor.uncovered} 只标的不在复权凭据覆盖范围内, 重算指标时会被逐个拒算 (job 结果的 uncovered 字段会列出)。`
			+ ' 这就是行数偏少的原因, 不是计算出错; 补齐同样要等下一轮定时同步。',
		)
	}
	if (daily && !enriched) {
		warnings.push('已有日K但没有 Enriched: 换手率/连板等下游列不存在。可点「重算指标」(纯本地计算, 不发网络请求)。')
	}

	const totalMb = storage?.total_size_mb ?? 0

	return (
		<div className="space-y-4">
			{/* 画像列表 */}
			<div className="space-y-1">
				{rows.map((r) => (
					<div
						key={r.key}
						className="flex items-center gap-2.5 rounded-btn border border-border bg-surface px-3 py-2"
					>
						<span className={r.present ? 'text-accent' : 'text-muted'}>
							{r.present
								? <CheckCircle2 className="h-3.5 w-3.5" />
								: <CircleDashed className="h-3.5 w-3.5" />}
						</span>
						<div className="min-w-0 flex-1">
							<div className="flex items-baseline justify-between gap-2">
								<span className="text-xs font-medium text-foreground">{r.label}</span>
								<span className="font-mono text-xs tabular-nums text-secondary">{r.main}</span>
							</div>
							<div className="flex items-baseline justify-between gap-2">
								<span className="truncate text-[10px] text-muted">{r.sub}</span>
								{r.note && (
									<span className="shrink-0 font-mono text-[10px] tabular-nums text-muted">{r.note}</span>
								)}
							</div>
						</div>
					</div>
				))}
			</div>

			{/* 告警 */}
			{warnings.length > 0 && (
				<div className="space-y-1.5">
					{warnings.map((w, i) => (
						<div
							key={i}
							className="flex items-start gap-1.5 rounded-btn border border-warning/20 bg-warning/10 px-3 py-2"
						>
							<AlertTriangle className="mt-px h-3 w-3 shrink-0 text-warning" />
							<span className="text-[10px] leading-relaxed text-warning">{w}</span>
						</div>
					))}
				</div>
			)}

			{/* 存储占用 */}
			<div className="flex items-center justify-between rounded-btn bg-elevated/50 px-3 py-2 text-[10px] text-muted">
				<span>港股数据占用</span>
				<span className="font-mono tabular-nums text-secondary">{totalMb.toFixed(1)} MB</span>
			</div>

			{/* 同步入口 */}
			<div className="space-y-2 border-t border-border pt-3">
				<div className="text-[10px] text-muted">
					同步顺序: 维表 → 日K (默认顺带算指标) → 需要时单独重算指标。日K与重算走重任务槽,
					与 A 股同步互斥。
				</div>
				<div className="grid grid-cols-3 gap-2">
					<button
						onClick={() => syncDim.mutate()}
						disabled={syncDim.isPending || isRunning}
						title="从东财拉取港股标的列表 (含双柜台/行业/每手股数), 秒级完成"
						className="inline-flex items-center justify-center gap-1.5 rounded-btn border border-border bg-surface px-2 py-1.5 text-xs text-foreground transition-colors hover:border-accent/50 disabled:pointer-events-none disabled:opacity-40"
					>
						{syncDim.isPending
							? <Loader2 className="h-3 w-3 animate-spin" />
							: <Table2 className="h-3 w-3" />}
						同步维表
					</button>
					<button
						onClick={() => syncDaily.mutate()}
						disabled={syncDaily.isPending || isRunning || !instruments}
						title={!instruments ? '维表为空, 请先同步维表' : '增量同步全市场港币柜台日K, 落库后紧接着重算指标'}
						className="inline-flex items-center justify-center gap-1.5 rounded-btn bg-accent/90 px-2 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-40"
					>
						{syncDaily.isPending
							? <Loader2 className="h-3 w-3 animate-spin" />
							: <RefreshCw className="h-3 w-3" />}
						同步日K+指标
					</button>
					<button
						onClick={() => syncEnriched.mutate()}
						disabled={syncEnriched.isPending || isRunning || !daily}
						title={!daily ? '还没有日K数据, 无从重算' : '纯本地计算, 不发网络请求; 缺复权凭据时整轮零行'}
						className="inline-flex items-center justify-center gap-1.5 rounded-btn border border-border bg-surface px-2 py-1.5 text-xs text-foreground transition-colors hover:border-accent/50 disabled:pointer-events-none disabled:opacity-40"
					>
						{syncEnriched.isPending
							? <Loader2 className="h-3 w-3 animate-spin" />
							: <RefreshCw className="h-3 w-3" />}
						重算指标
					</button>
				</div>
				{isRunning && (
					<div className="text-[10px] text-muted">
						已有数据任务在跑, 同步入口暂时禁用 (进度见页面顶部的任务卡)。
					</div>
				)}
			</div>
		</div>
	)
}
