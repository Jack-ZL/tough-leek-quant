import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
	AlertTriangle,
	CheckCircle2,
	Clock3,
	Dna,
	LoaderCircle,
	Play,
	RefreshCw,
	Sparkles,
	Square,
	Trash2,
} from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { toast } from '@/components/Toast'
import {
	api,
	type EvolutionRun,
	type EvolutionStartPayload,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const INPUT = 'h-8 w-full rounded-input border border-border bg-surface px-2 text-xs text-foreground outline-none transition-colors focus:border-accent'
const LABEL = 'mb-1 block text-[10px] font-medium text-secondary'
const SUCCESS = new Set(['succeeded'])
const ACTIVE = new Set(['queued', 'running', 'cancelling'])

interface Draft {
	assetType: 'stock' | 'etf'
	populationSize: number
	generations: number
	horizon: number
	maxSymbols: number
	maxDepth: number
	start: string
	end: string
	seed: string
	llmPropose: boolean
	llmCount: number
	force: boolean
}

const DEFAULT_DRAFT: Draft = {
	assetType: 'stock',
	populationSize: 120,
	generations: 12,
	horizon: 5,
	maxSymbols: 600,
	maxDepth: 4,
	start: '',
	end: '',
	seed: '',
	llmPropose: false,
	llmCount: 12,
	force: false,
}

const DRAFT_KEY = 'evolution_workbench_draft_v1'

function loadDraft(): Draft {
	try {
		const raw = localStorage.getItem(DRAFT_KEY)
		if (raw) return { ...DEFAULT_DRAFT, ...(JSON.parse(raw) as Partial<Draft>) }
	} catch { /* ignore */ }
	return DEFAULT_DRAFT
}

function formatNumber(value: number | null | undefined, digits = 2) {
	if (value == null || !Number.isFinite(value)) return '—'
	return value.toFixed(digits)
}

function formatPct(value: number | null | undefined, digits = 1) {
	if (value == null || !Number.isFinite(value)) return '—'
	return `${(value * 100).toFixed(digits)}%`
}

function statusLabel(status?: string) {
	switch (status) {
		case 'queued': return '排队中'
		case 'running': return '运行中'
		case 'cancelling': return '取消中'
		case 'cancelled': return '已取消'
		case 'succeeded': return '已完成'
		case 'failed': return '失败'
		default: return status || '—'
	}
}

function StatusIcon({ status }: { status?: string }) {
	if (status && ACTIVE.has(status)) return <LoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
	if (status && SUCCESS.has(status)) return <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" />
	if (status === 'failed') return <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-danger" />
	return <Clock3 className="h-3.5 w-3.5 shrink-0 text-muted" />
}

export function EvolutionWorkbench() {
	const queryClient = useQueryClient()
	const [draft, setDraft] = useState<Draft>(loadDraft)
	const [selectedRunId, setSelectedRunId] = useState<string | null>(null)

	useEffect(() => {
		localStorage.setItem(DRAFT_KEY, JSON.stringify(draft))
	}, [draft])

	const runsQuery = useQuery({
		queryKey: QK.evolutionRuns,
		queryFn: () => api.evolutionRuns(30),
		refetchInterval: query => {
			const items = query.state.data?.items ?? []
			return items.some(run => ACTIVE.has(run.status)) ? 4000 : false
		},
	})

	// 默认选中最近一个 run
	useEffect(() => {
		if (!selectedRunId && runsQuery.data?.items.length) {
			setSelectedRunId(runsQuery.data.items[0].run_id)
		}
	}, [runsQuery.data, selectedRunId])

	const selectedInList = useMemo(
		() => runsQuery.data?.items.find(run => run.run_id === selectedRunId) ?? null,
		[runsQuery.data, selectedRunId],
	)
	const selectedActive = !!selectedInList && ACTIVE.has(selectedInList.status)

	const runQuery = useQuery({
		queryKey: QK.evolutionRun(selectedRunId ?? ''),
		queryFn: () => api.evolutionRun(selectedRunId!),
		enabled: !!selectedRunId,
		refetchInterval: selectedActive ? 4000 : false,
	})

	const run: EvolutionRun | null = runQuery.data ?? selectedInList

	const start = useMutation({
		mutationFn: (payload: EvolutionStartPayload) => api.evolutionStart(payload),
		onSuccess: created => {
			queryClient.invalidateQueries({ queryKey: QK.evolutionRuns })
			setSelectedRunId(created.run_id)
			toast('进化任务已启动', 'success')
		},
		onError: error => toast(`启动失败 · ${String((error as Error).message || error)}`, 'error'),
	})

	const cancel = useMutation({
		mutationFn: (runId: string) => api.evolutionCancel(runId),
		onSuccess: () => {
			queryClient.invalidateQueries({ queryKey: QK.evolutionRuns })
			if (selectedRunId) queryClient.invalidateQueries({ queryKey: QK.evolutionRun(selectedRunId) })
			toast('已请求取消', 'success')
		},
		onError: error => toast(`取消失败 · ${String((error as Error).message || error)}`, 'error'),
	})

	const publish = useMutation({
		mutationFn: ({ runId, index }: { runId: string; index: number }) => api.evolutionPublish(runId, index),
		onSuccess: result => {
			toast(`已发布草稿因子 · ${result.id}`, 'success')
		},
		onError: error => toast(`发布失败 · ${String((error as Error).message || error)}`, 'error'),
	})

	const submit = () => {
		if (draft.start && draft.end && draft.start >= draft.end) {
			toast('开始日期必须早于结束日期', 'error')
			return
		}
		const payload: EvolutionStartPayload = {
			asset_type: draft.assetType,
			population_size: draft.populationSize,
			generations: draft.generations,
			horizon: draft.horizon,
			max_symbols: draft.maxSymbols,
			max_depth: draft.maxDepth,
			force: draft.force,
		}
		if (draft.start) payload.start = draft.start
		if (draft.end) payload.end = draft.end
		if (draft.seed.trim()) payload.seed = Number(draft.seed.trim())
		if (draft.llmPropose) {
			payload.llm_propose = true
			payload.llm_count = Math.min(30, Math.max(5, draft.llmCount || 12))
		}
		start.mutate(payload)
	}

	const result = run?.result ?? null
	const progress = run?.progress ?? null
	const percent = progress && progress.total
		? Math.round(((progress.done ?? 0) / progress.total) * 100)
		: null

	return (
		<div className="grid min-h-[calc(100vh-9rem)] grid-cols-1 overflow-hidden rounded-card border border-border bg-surface xl:grid-cols-[20rem_minmax(0,1fr)]">
			{/* 左侧: 启动表单 + 运行历史 */}
			<aside className="flex min-h-0 flex-col gap-3 overflow-y-auto border-b border-border bg-base/20 p-3 xl:max-h-[calc(100vh-9rem)] xl:border-b-0 xl:border-r">
				<div className="flex items-center gap-2">
					<Dna className="h-4 w-4 text-accent" />
					<span className="text-xs font-semibold text-foreground">公式进化 (GP)</span>
				</div>
				<p className="text-[10px] leading-4 text-muted">
					遗传算法在 DSL 公式空间里"养蛊": 随机生成 → 训练窗 IC 适应度筛选 → 交叉/变异繁殖。
					幸存者用<span className="text-secondary"> 保留窗</span>一次性裁决 (NW-t / BH-FDR / DSR)，发布为草稿因子后再走既有挖掘漏斗深度验证。
				</p>

				<div className="grid grid-cols-2 gap-2">
					<label className="col-span-2">
						<span className={LABEL}>资产类型</span>
						<select className={INPUT} value={draft.assetType} onChange={e => setDraft({ ...draft, assetType: e.target.value as 'stock' | 'etf' })}>
							<option value="stock">股票</option>
							<option value="etf">ETF</option>
						</select>
					</label>
					<label>
						<span className={LABEL}>种群规模</span>
						<input type="number" min={20} max={400} className={INPUT} value={draft.populationSize}
							onChange={e => setDraft({ ...draft, populationSize: Number(e.target.value) })} />
					</label>
					<label>
						<span className={LABEL}>迭代代数</span>
						<input type="number" min={2} max={40} className={INPUT} value={draft.generations}
							onChange={e => setDraft({ ...draft, generations: Number(e.target.value) })} />
					</label>
					<label>
						<span className={LABEL}>预测周期 (日)</span>
						<input type="number" min={1} max={20} className={INPUT} value={draft.horizon}
							onChange={e => setDraft({ ...draft, horizon: Number(e.target.value) })} />
					</label>
					<label>
						<span className={LABEL}>最大标的数</span>
						<input type="number" min={50} max={2000} step={50} className={INPUT} value={draft.maxSymbols}
							onChange={e => setDraft({ ...draft, maxSymbols: Number(e.target.value) })} />
					</label>
					<label>
						<span className={LABEL}>表达式最大深度</span>
						<input type="number" min={2} max={6} className={INPUT} value={draft.maxDepth}
							onChange={e => setDraft({ ...draft, maxDepth: Number(e.target.value) })} />
					</label>
					<label>
						<span className={LABEL}>随机种子 (可选)</span>
						<input type="text" inputMode="numeric" placeholder="留空随机" className={INPUT} value={draft.seed}
							onChange={e => setDraft({ ...draft, seed: e.target.value.replace(/[^0-9]/g, '') })} />
					</label>
					<label>
						<span className={LABEL}>开始日期 (可选)</span>
						<input type="date" className={INPUT} value={draft.start}
							onChange={e => setDraft({ ...draft, start: e.target.value })} />
					</label>
					<label>
						<span className={LABEL}>结束日期 (可选)</span>
						<input type="date" className={INPUT} value={draft.end}
							onChange={e => setDraft({ ...draft, end: e.target.value })} />
					</label>
					<label className="col-span-2 flex items-center gap-1.5 text-[10px] text-secondary">
						<input type="checkbox" className="h-3 w-3 accent-accent" checked={draft.llmPropose}
							onChange={e => setDraft({ ...draft, llmPropose: e.target.checked })} />
						AI 提议公式 (LLM 出种子, 同一套统计纪律裁决; 失败自动降级纯 GP)
					</label>
					{draft.llmPropose && (
						<label className="col-span-2">
							<span className={LABEL}>AI 提议条数 (5-30)</span>
							<input type="number" min={5} max={30} className={INPUT} value={draft.llmCount}
								onChange={e => setDraft({ ...draft, llmCount: Number(e.target.value) })} />
						</label>
					)}
					<label className="col-span-2 flex items-center gap-1.5 text-[10px] text-secondary">
						<input type="checkbox" className="h-3 w-3 accent-accent" checked={draft.force}
							onChange={e => setDraft({ ...draft, force: e.target.checked })} />
						强制新建 (忽略同参数去重)
					</label>
				</div>

				<div className="flex gap-2">
					<button type="button" disabled={start.isPending} onClick={submit}
						className="inline-flex h-8 flex-1 items-center justify-center gap-1.5 rounded-btn bg-accent text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50">
						{start.isPending ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
						开始进化
					</button>
					{selectedActive && (
						<button type="button" disabled={cancel.isPending} onClick={() => selectedRunId && cancel.mutate(selectedRunId)}
							title="取消当前运行"
							className="inline-flex h-8 items-center justify-center gap-1 rounded-btn border border-border px-2.5 text-xs text-secondary transition-colors hover:border-danger/40 hover:text-danger disabled:opacity-50">
							<Square className="h-3.5 w-3.5" />
						</button>
					)}
				</div>

				<section className="border-t border-border pt-3">
					<div className="mb-2 flex items-center justify-between">
						<span className="text-[10px] font-semibold text-secondary">最近运行</span>
						<button type="button" title="刷新历史" onClick={() => void runsQuery.refetch()} className="text-muted hover:text-accent">
							<RefreshCw className={`h-3 w-3 ${runsQuery.isFetching ? 'animate-spin' : ''}`} />
						</button>
					</div>
					<div className="max-h-56 space-y-1 overflow-y-auto">
						{(runsQuery.data?.items ?? []).map(item => (
							<button key={item.run_id} type="button" onClick={() => setSelectedRunId(item.run_id)}
								className={`flex w-full items-center gap-2 rounded-btn px-2 py-1.5 text-left hover:bg-elevated ${selectedRunId === item.run_id ? 'bg-accent/10' : ''}`}>
								<span className={`h-1.5 w-1.5 shrink-0 rounded-full ${SUCCESS.has(item.status) ? 'bg-success' : ACTIVE.has(item.status) ? 'bg-accent' : item.status === 'failed' ? 'bg-danger' : 'bg-muted'}`} />
								<span className="min-w-0 flex-1 truncate font-mono text-[9px] text-secondary">{item.run_id}</span>
								{typeof item.survivor_count === 'number' && item.survivor_count > 0 &&
									<span className="shrink-0 text-[9px] text-muted">{item.survivor_count} 存活</span>}
								<span className="shrink-0 text-[9px] text-muted">{statusLabel(item.status)}</span>
							</button>
						))}
						{runsQuery.isError && <div className="text-[9px] text-danger">运行历史加载失败</div>}
						{!runsQuery.isLoading && !runsQuery.isError && !runsQuery.data?.items.length &&
							<div className="text-[9px] text-muted">暂无进化运行</div>}
					</div>
				</section>
			</aside>

			{/* 右侧: 详情 */}
			<section className="min-w-0 bg-surface xl:max-h-[calc(100vh-9rem)] xl:overflow-y-auto">
				{/* 状态条 */}
				<div className={`flex min-w-0 items-center gap-2 border-b border-border px-3 py-2 ${run?.error ? 'bg-danger/5' : 'bg-base/20'}`}>
					<StatusIcon status={run?.status} />
					<div className="min-w-0 flex-1">
						<div className="truncate text-[11px] font-medium text-foreground">
							{run ? statusLabel(run.status) : '公式进化'}
							{progress?.label ? ` · ${progress.label}` : ''}
						</div>
						{(run?.error || progress?.phase) && (
							<div className={`mt-0.5 text-[9px] leading-4 ${run?.error ? 'whitespace-pre-wrap break-words text-danger' : 'truncate text-muted'}`} title={run?.error || progress?.phase}>
								{run?.error || progress?.phase}
							</div>
						)}
					</div>
					{percent != null && <span className="shrink-0 font-mono text-[10px] text-secondary">{percent}%</span>}
					{run?.run_id && <span className="hidden max-w-40 truncate font-mono text-[9px] text-muted sm:block">{run.run_id}</span>}
				</div>

				{!run ? (
					<div className="min-h-[32rem]">
						<EmptyState icon={Dna} title="尚无进化结果" hint="在左侧设置种群规模、代数与预测周期后点击「开始进化」。任务在独立 worker 中运行，可切换页面或刷新后按 run ID 重连。" />
					</div>
				) : !result ? (
					<div className="min-h-[32rem]">
						<EmptyState icon={Dna} title={ACTIVE.has(run.status) ? '进化任务正在执行' : '该运行无结果'} hint={ACTIVE.has(run.status) ? '正在加载行情面板并逐代进化，请稍候…' : '运行未成功完成，无幸存者数据。'} />
					</div>
				) : (
					<div className="space-y-4 p-3">
						{/* 面板 + 试验概览 */}
						<div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
							<Stat label="标的数" value={`${result.panel.n_symbols}${result.panel.universe_total > result.panel.n_symbols ? ` / ${result.panel.universe_total}` : ''}`} />
							<Stat label="交易日" value={`${result.panel.n_days}`} sub={`训练 ${result.panel.train_days} · 保留 ${result.panel.holdout_days}`} />
							<Stat label="试验次数" value={`${result.trials.evaluated}`} sub={`重复 ${result.trials.duplicates} · 无效 ${result.trials.invalid}`} />
							<Stat label="耗时" value={`${(result.elapsed_ms / 1000).toFixed(1)}s`} sub={result.algorithm_version} />
						</div>
						<div className="rounded-card border border-border bg-base/20 px-3 py-2 text-[10px] leading-4 text-muted">
							训练窗 {result.panel.train_start} ~ {result.panel.train_end} · 保留窗 {result.panel.holdout_start} ~ {result.panel.holdout_end}
							{result.dsr.expected_max_sharpe != null &&
								<> · 期望最大 Sharpe {formatNumber(result.dsr.expected_max_sharpe, 3)} (试验方差 {formatNumber(result.dsr.variance_sharpes, 4)})</>}
						</div>

						{/* AI 提议通道 (P2-22b) */}
						{result.llm && (
							<div className="rounded-card border border-border bg-base/20 p-3">
								<div className="mb-1.5 flex flex-wrap items-baseline gap-x-2 text-[10px] font-semibold text-secondary">
									<span className="inline-flex items-center gap-1"><Sparkles className="h-3 w-3 text-accent" />AI 提议公式</span>
									<span className="font-normal text-muted">
										请求 {result.llm.requested} · 提议 {result.llm.proposed} · 接受 {result.llm.accepted.length} · 拒绝 {result.llm.rejected.length}
										{result.llm.repair_rounds > 0 && <> · 修复 {result.llm.repair_rounds} 轮</>}
										{result.llm.attribution?.model && <> · 模型 {result.llm.attribution.model}</>}
										{result.llm.elapsed_ms != null && <> · {(result.llm.elapsed_ms / 1000).toFixed(1)}s</>}
									</span>
								</div>
								{result.llm.error && (
									<div className="mb-1.5 flex items-start gap-1 rounded-input border border-warning/30 bg-warning/5 px-2 py-1 text-[10px] text-warning">
										<AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
										<span>{result.llm.error}（已降级为纯 GP，任务照常完成）</span>
									</div>
								)}
								{result.llm.accepted.length > 0 && (
									<div className="space-y-1">
										{result.llm.accepted.map(item => (
											<div key={item.formula} className="flex items-baseline gap-2 text-[10px]">
												<span className="shrink-0 font-mono text-foreground">{item.formula}</span>
												{item.direction === 'high' && <span className="shrink-0 text-[9px] text-muted">预期正相关</span>}
												{item.direction === 'low' && <span className="shrink-0 text-[9px] text-muted">预期负相关</span>}
												{item.rationale && <span className="truncate text-[9px] text-muted" title={item.rationale}>{item.rationale}</span>}
											</div>
										))}
									</div>
								)}
								{result.llm.rejected.length > 0 && (
									<details className="mt-1.5">
										<summary className="cursor-pointer text-[9px] text-muted">被拒提案 ({result.llm.rejected.length})</summary>
										<div className="mt-1 space-y-0.5">
											{result.llm.rejected.map((item, index) => (
												<div key={`${item.formula}-${index}`} className="flex items-baseline gap-2 text-[9px] text-muted">
													<span className="shrink-0 font-mono">{item.formula}</span>
													<span className="truncate" title={item.reason}>{item.reason}</span>
												</div>
											))}
										</div>
									</details>
								)}
							</div>
						)}

						{/* 逐代适应度 */}
						{result.generations.length > 0 && (
							<div>
								<div className="mb-1.5 text-[10px] font-semibold text-secondary">逐代适应度 (训练窗 IC-IR)</div>
								<div className="overflow-x-auto rounded-card border border-border">
									<div className="min-w-[420px]">
										<div className="grid grid-cols-[48px_repeat(3,minmax(72px,1fr))_minmax(160px,2fr)] border-b border-border bg-base/50 px-2 py-1.5 text-[9px] font-medium text-muted">
											<span>代数</span><span>最佳</span><span>均值</span><span>累计试验</span><span>最佳公式</span>
										</div>
										{result.generations.map(gen => (
											<div key={gen.gen} className="grid grid-cols-[48px_repeat(3,minmax(72px,1fr))_minmax(160px,2fr)] border-b border-border/50 px-2 py-1 text-[10px] text-secondary last:border-b-0">
												<span className="font-mono">{gen.gen}</span>
												<span className="font-mono text-foreground">{formatNumber(gen.best, 3)}</span>
												<span className="font-mono">{formatNumber(gen.mean, 3)}</span>
												<span className="font-mono">{gen.trials_total}</span>
												<span className="truncate font-mono text-[9px] text-muted" title={gen.best_formula}>{gen.best_formula}</span>
											</div>
										))}
									</div>
								</div>
							</div>
						)}

						{/* 幸存者 */}
						<div>
							<div className="mb-1.5 flex items-center gap-2 text-[10px] font-semibold text-secondary">
								幸存者 ({result.survivors.length})
								<span className="font-normal text-muted">保留窗一次性裁决 · 相关性 &gt;0.8 已剪枝</span>
							</div>
							{result.survivors.length === 0 ? (
								<div className="rounded-card border border-border bg-base/20 px-3 py-4 text-center text-[10px] text-muted">无幸存者通过保留窗裁决</div>
							) : (
								<div className="overflow-x-auto rounded-card border border-border">
									<div className="min-w-[900px]">
										<div className="grid grid-cols-[minmax(220px,2fr)_52px_repeat(3,minmax(58px,1fr))_repeat(5,minmax(64px,1fr))_88px] border-b border-border bg-base/50 px-2 py-1.5 text-[9px] font-medium text-muted">
											<span>公式</span><span>方向</span>
											<span>训练IC</span><span>训练IR</span><span>适应度</span>
											<span>保留IC</span><span>保留IR</span><span>NW-t</span><span>q值</span><span>DSR</span>
											<span>操作</span>
										</div>
										{result.survivors.map((survivor, index) => (
											<div key={survivor.key} className="grid grid-cols-[minmax(220px,2fr)_52px_repeat(3,minmax(58px,1fr))_repeat(5,minmax(64px,1fr))_88px] items-center border-b border-border/50 px-2 py-1.5 text-[10px] text-secondary last:border-b-0 hover:bg-elevated/40">
												<span className="truncate font-mono text-[9px] text-foreground" title={survivor.formula}>{survivor.formula}</span>
												<span className={survivor.direction === 'high' ? 'text-success' : 'text-danger'}>{survivor.direction === 'high' ? '正向' : '反向'}</span>
												<span className="font-mono">{formatNumber(survivor.train.ic_mean, 4)}</span>
												<span className="font-mono">{formatNumber(survivor.train.ir, 2)}</span>
												<span className="font-mono">{formatNumber(survivor.train.fitness, 3)}</span>
												<span className="font-mono">{formatNumber(survivor.holdout.ic_mean, 4)}</span>
												<span className="font-mono">{formatNumber(survivor.holdout.ir, 2)}</span>
												<span className="font-mono">{formatNumber(survivor.holdout.t_nw, 2)}</span>
												<span className="font-mono">{formatNumber(survivor.holdout.q_value, 3)}</span>
												<span className="font-mono">{formatNumber(survivor.holdout.dsr, 3)}</span>
												<button type="button" disabled={publish.isPending}
													onClick={() => publish.mutate({ runId: run.run_id, index })}
													title={survivor.holdout.coverage != null ? `保留窗覆盖 ${formatPct(survivor.holdout.coverage)}` : '发布为草稿因子'}
													className="inline-flex h-6 items-center justify-center gap-1 rounded-btn border border-border px-2 text-[9px] text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:opacity-50">
													{publish.isPending && publish.variables?.index === index
														? <LoaderCircle className="h-3 w-3 animate-spin" /> : <Dna className="h-3 w-3" />}
													发布
												</button>
											</div>
										))}
									</div>
								</div>
							)}
						</div>

						{/* 剪枝 */}
						{result.pruned.length > 0 && (
							<div>
								<div className="mb-1.5 text-[10px] font-semibold text-secondary">剪枝 ({result.pruned.length})</div>
								<div className="space-y-1">
									{result.pruned.map(item => (
										<div key={item.key} className="flex items-center gap-2 rounded-btn bg-base/20 px-2 py-1 text-[9px]">
											<Trash2 className="h-3 w-3 shrink-0 text-muted" />
											<span className="min-w-0 flex-1 truncate font-mono text-secondary" title={item.formula}>{item.formula}</span>
											<span className="shrink-0 text-muted">{item.reason}</span>
										</div>
									))}
								</div>
							</div>
						)}
					</div>
				)}
			</section>
		</div>
	)
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
	return (
		<div className="rounded-card border border-border bg-base/20 px-3 py-2">
			<div className="text-[9px] text-muted">{label}</div>
			<div className="mt-0.5 font-mono text-sm font-medium text-foreground">{value}</div>
			{sub && <div className="mt-0.5 text-[9px] text-muted">{sub}</div>}
		</div>
	)
}
