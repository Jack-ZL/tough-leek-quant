import { useEffect, useRef, useState, useCallback } from 'react'
import {
	X, Sparkles, Loader2, AlertTriangle, Copy, Check, RefreshCw,
	Settings2, Send, Wand2, Minimize2, History, LineChart, Swords, ChevronDown,
} from 'lucide-react'
import { cn } from '@/lib/cn'
import { copyText } from '@/lib/clipboard'
import { api, type AiScorecard } from '@/lib/api'
import { toast } from '@/components/Toast'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
// P1-11 数据体检徽章 (个股分析/多空辩论共用)
import { QualityBadge } from './QualityBadge'
import {
	type ActiveTask, type HistoryReport,
	minimizeDialog, closeDialog, startAnalysis, verdictTone,
} from '@/lib/stockAnalysisStore'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { openDebate } from '@/lib/debateStore'

/**
 * AI 个股分析对话框 —— 蓝色主题,与财务分析对话框区分。
 * 复用 MarkdownRenderer(通用 markdown 渲染);标题/配色/文案独立。
 */

interface Props {
	task: ActiveTask | HistoryReport | null
	mode: 'active' | 'history' | null
	minimized: boolean
}

type Phase = 'loading' | 'streaming' | 'done' | 'error'

function getPhase(task: ActiveTask | HistoryReport | null): Phase {
	if (!task) return 'loading'
	if ('phase' in task) return task.phase
	return 'done'
}

function getContent(task: ActiveTask | HistoryReport | null): string {
	return task?.content ?? ''
}

function getMeta(task: ActiveTask | HistoryReport | null) {
	if (!task) return null
	if ('meta' in task) return task.meta
	// P1-11: 历史报告的体检快照是顶层字段, 与活跃任务 meta.quality 对齐
	return { summary: task.summary, close: task.close, levels: task.levels, quality: task.quality ?? null }
}

export function StockAnalysisDialog({ task, mode, minimized }: Props) {
	const scrollRef = useRef<HTMLDivElement>(null)
	const [focus, setFocus] = useState('')
	const [copied, setCopied] = useState(false)
	// P0-7 记分卡: 对话框打开时拉一次 (后端顺手惰性结算); 失败静默 — 战绩是锦上添花不阻塞分析
	const [scorecard, setScorecard] = useState<AiScorecard | null>(null)
	// P1-10: chip 点击展开多周期战绩面板
	const [scorecardOpen, setScorecardOpen] = useState(false)

	const phase = getPhase(task)
	const content = getContent(task)
	const meta = getMeta(task)
	// 研判卡: 活跃任务来自 verdict 事件, 历史报告来自落库字段
	const verdict = task && 'verdict' in task ? task.verdict : null
	const isHistory = mode === 'history'
	const isWorking = phase === 'loading' || phase === 'streaming'
	const open = !!task && !minimized

	useEffect(() => {
		if (open && phase === 'streaming' && scrollRef.current) {
			scrollRef.current.scrollTop = scrollRef.current.scrollHeight
		}
	}, [content, phase, open])

	useEffect(() => {
		setFocus(task && 'focus' in task ? task.focus : '')
	}, [task])

	// 打开对话框时拉取记分卡 (每次打开都拉 — 后端惰性结算, 战绩随时间推进)
	useEffect(() => {
		if (!open) return
		let cancelled = false
		api.stockAnalysisScorecard()
			.then((r) => {
				if (!cancelled && r.ok) setScorecard(r.scorecard)
			})
			.catch(() => { /* 静默失败: 记分卡是辅助信息, 不影响分析主流程 */
			})
		return () => {
			cancelled = true
		}
	}, [open])

	const handleStartNew = useCallback(async () => {
		if (!task) return
		const name = 'name' in task ? task.name : ''
		await startAnalysis(task.symbol, name, focus.trim())
	}, [task, focus])

	const handleCopy = async () => {
		if (!content) return
		const success = await copyText(content)
		if (!success) {
			toast('复制失败,请手动选择文本', 'error')
			return
		}
		setCopied(true)
		toast('已复制到剪贴板', 'success')
		setTimeout(() => setCopied(false), 2000)
	}

	const backdrop = useDialogBackdrop(closeDialog, () => !isWorking)

	if (!open) return null

	const error = task && 'error' in task ? task.error : ''

	// 入场动画说明(修「打开历史报告闪一下」):
	//  - 遮罩直接挂载, 不做透明度动画 — backdrop-filter 元素动画 opacity 会逐帧重采样模糊;
	//  - 卡片改用纯 CSS transform/opacity 动画(index.css 的 dialog-pop) — 合成器线程驱动,
	//    主线程忙(历史报告整篇正文首帧渲染 / 行情推送触发的重渲染)也不掉帧;
	//  - 去掉卡片 backdrop-blur-xl — 卡片本就 95% 不透明, 模糊几乎不可见, 却让动画每帧
	//    在 ECharts canvas 上重算模糊、首帧还要新建合成层, 真实 GPU 环境下易闪/卡。
	//  原弹簧动画为主线程 JS 驱动, 上述任一负载都会让它掉帧(无头浏览器逐帧 screencast
	//  无法复现, 属真实环境合成/主线程问题, 故从机制上消除)。
	return (
		<div
			className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
			{...backdrop}
		>
			<div className="dialog-pop w-full max-w-3xl max-h-[88vh] bg-surface/95 border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden">
				{/* 头部 —— 蓝色主题 */}
				<div className="relative px-5 py-3.5 border-b border-border/50 bg-gradient-to-r from-sky-500/[0.06] via-blue-500/[0.04] to-transparent">
					<div className="flex items-center gap-3">
						<div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500/20 to-blue-500/15 border border-sky-400/30 shrink-0">
							{isHistory
								? <History className="h-4.5 w-4.5 text-sky-300" />
								: <LineChart className="h-4.5 w-4.5 text-sky-300" />}
						</div>
						<div className="min-w-0 flex-1">
							<div className="flex items-center gap-2">
								<span className="text-sm font-semibold text-foreground truncate">
									{isHistory ? '历史分析报告' : 'AI 个股分析'}
								</span>
								{task && <span className="text-xs text-secondary truncate">{task.name}</span>}
								{task && <span className="text-[10px] font-mono text-muted shrink-0">{task.symbol}</span>}
							</div>
							<div className="flex items-center gap-2 mt-0.5 text-[10px] text-muted">
								{meta?.summary ? (
									<span className="flex items-center gap-1 truncate">
										<Sparkles className="h-2.5 w-2.5 shrink-0" />
										<span className="truncate">{meta.summary}</span>
									</span>
								) : isWorking ? <span>正在读取行情与价位数据…</span> : null}
								{phase === 'streaming' && (
									<span className="flex items-center gap-1 text-sky-300 shrink-0">
										<span className="h-1.5 w-1.5 rounded-full bg-sky-400 animate-pulse" />生成中
									</span>
								)}
								{isHistory && task && 'created_at' in task && (
									<span className="shrink-0">{fmtRelative(task.created_at)}</span>
								)}
							</div>
						</div>
						<div className="flex items-center gap-1 shrink-0">
							{task && (
								<button onClick={() => openDebate(task.symbol, task.name)} title="多空辩论 — 历史与发起入口"
									className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-orange-300 transition-colors">
									<Swords className="h-4 w-4" />
								</button>
							)}
							{content && !isWorking && (
								<button onClick={handleCopy} title="复制全文"
									className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
									{copied ? <Check className="h-4 w-4 text-emerald-400" /> : <Copy className="h-4 w-4" />}
								</button>
							)}
							{!isHistory && isWorking && (
								<button onClick={minimizeDialog} title="最小化为气泡,后台继续生成"
									className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
									<Minimize2 className="h-4 w-4" />
								</button>
							)}
							{(!isWorking || isHistory) && (
								<button onClick={closeDialog} title="关闭"
									className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
									<X className="h-4 w-4" />
								</button>
							)}
						</div>
					</div>
				</div>

				{/* 内容区 */}
				<div ref={scrollRef} className="flex-1 overflow-y-auto px-5 py-4 min-h-[280px]">
					{phase === 'loading' && !content && (
						<div className="flex flex-col items-center justify-center py-16 gap-3">
							<div className="relative">
								<div className="h-10 w-10 rounded-full bg-gradient-to-br from-sky-500/20 to-blue-500/15 border border-sky-400/30 flex items-center justify-center">
									<LineChart className="h-4.5 w-4.5 text-sky-300 animate-pulse" />
								</div>
								<Loader2 className="absolute -inset-1 h-12 w-12 text-sky-400/40 animate-spin" style={{ animationDuration: '3s' }} />
							</div>
							<div className="text-xs text-secondary">AI 正在分析行情与关键价位…</div>
							<div className="text-[10px] text-muted">读取日 K / 技术指标 / 压力支撑 / 财务,生成四维分析</div>
						</div>
					)}

					{phase === 'error' && (
						<div className="flex flex-col items-center justify-center py-14 gap-3">
							<div className="h-11 w-11 rounded-full bg-danger/10 flex items-center justify-center">
								<AlertTriangle className="h-5 w-5 text-danger" />
							</div>
							<div className="text-sm font-medium text-foreground">分析失败</div>
							<div className="text-xs text-secondary text-center max-w-md px-4">{error}</div>
							{error.includes('AI') && (
								<button onClick={() => {
									window.location.href = `${import.meta.env.BASE_URL}settings?tab=ai`
								}}
									className="mt-1 inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-elevated border border-border text-xs text-secondary hover:text-foreground transition-colors">
									<Settings2 className="h-3.5 w-3.5" /> 去配置 AI
								</button>
							)}
							<button onClick={handleStartNew}
								className="mt-1 inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-sky-500/15 border border-sky-400/30 text-xs text-sky-300 hover:bg-sky-500/20 transition-colors">
								<RefreshCw className="h-3.5 w-3.5" /> 重试
							</button>
						</div>
					)}

					{(content || phase === 'streaming') && (
						<div className="relative">
							{/* 研判卡徽章: AI 按答题卡格式交的机读结论; ok=false 明示未解析, 不猜 */}
							{verdict ? (
								<div className={cn(
									'mb-3 flex items-center gap-2 rounded-lg border px-3 py-2 text-xs',
									verdict.ok && verdictTone(verdict.direction) === 'bull' && 'border-bull/30 bg-bull/10',
									verdict.ok && verdictTone(verdict.direction) === 'bear' && 'border-bear/30 bg-bear/10',
									verdict.ok && verdictTone(verdict.direction) === 'neutral' && 'border-border/60 bg-elevated/40',
									!verdict.ok && 'border-border/40 bg-elevated/20',
								)}>
									<span className="shrink-0 text-muted">🧭 AI 研判</span>
									{verdict.ok ? (
										<>
											<span className={cn(
												'font-semibold shrink-0',
												verdictTone(verdict.direction) === 'bull' && 'text-bull',
												verdictTone(verdict.direction) === 'bear' && 'text-bear',
												verdictTone(verdict.direction) === 'neutral' && 'text-secondary',
											)}>{verdict.direction}</span>
											{verdict.reason &&
												<span className="text-secondary truncate">—— {verdict.reason}</span>}
										</>
									) : (
										<span className="text-muted">本次未解析出研判结论</span>
									)}
								</div>
							) : isHistory ? (
								/* 旧报告(研判卡上线前/旧页面生成)落库无 verdict — 明示原因, 避免误以为功能坏了 */
								<div className="mb-3 flex items-center gap-2 rounded-lg border border-border/40 bg-elevated/20 px-3 py-2 text-xs text-muted">
									<span className="shrink-0">🧭 AI 研判</span>
									<span>该报告生成时未记录研判卡, 点「重新生成」即可获取</span>
								</div>
							) : null}
							{/* P1-10 记分卡: chip 摘要 (T+5 口径) 点击展开多周期战绩面板; 样本不足明说, 不给误导百分比 */}
							{scorecard && Object.values(scorecard.horizons).some(h => h.settled > 0) && (
								<div className="mb-3">
									<button
										onClick={() => setScorecardOpen(v => !v)}
										className="flex w-full items-center gap-2 rounded-lg border border-border/40 bg-elevated/20 px-3 py-1.5 text-xs text-muted transition-colors hover:text-secondary"
										title={`方向事后统计(研判日起第 1/3/5/10 个交易日收盘对照), 不是回测; 点击${scorecardOpen ? '收起' : '展开'}多周期战绩`}
									>
										<span className="shrink-0">📊 AI 历史战绩</span>
										{scorecard.insufficient || scorecard.hit_rate_pct == null ? (
											<span className="truncate">T+{scorecard.horizon_days} 样本不足 (已结算 {scorecard.settled} 次, 满 {scorecard.min_sample} 次后统计命中率)</span>
										) : (
											<span className="truncate">
												T+{scorecard.horizon_days} 命中率{' '}
												<span className="font-semibold text-secondary">{scorecard.hit_rate_pct}%</span>
												{' '}({scorecard.settled} 次结算判对 {scorecard.hits} 次)
												<span className="ml-1 opacity-70">· 方向事后统计, 非回测</span>
											</span>
										)}
										<ChevronDown className={cn('ml-auto h-3.5 w-3.5 shrink-0 transition-transform', scorecardOpen && 'rotate-180')} />
									</button>
									{scorecardOpen && <ScorecardPanel sc={scorecard} />}
								</div>
							)}
							{/* P1-11 数据体检徽章: 报告生成时的数据质量评级, 点击展开问题清单 */}
							{meta?.quality && <QualityBadge quality={meta.quality} />}
							<MarkdownRenderer content={content} />
							{phase === 'streaming' && (
								<span className="inline-block w-1.5 h-3.5 bg-sky-400 ml-0.5 align-middle animate-pulse rounded-sm" />
							)}
						</div>
					)}
				</div>

				{/* 底部:关注点输入 */}
				<div className="border-t border-border/50 bg-surface/60 px-5 py-3">
					<div className="flex items-center gap-2">
						<div className="flex items-center gap-1.5 text-[10px] text-muted shrink-0">
							<Wand2 className="h-3 w-3" />
							<span className="hidden sm:inline">关注重点</span>
						</div>
						<input
							type="text"
							value={focus}
							onChange={e => setFocus(e.target.value)}
							onKeyDown={e => {
								if (e.key === 'Enter' && (phase === 'done' || phase === 'error' || isHistory)) handleStartNew()
							}}
							disabled={isWorking}
							placeholder={isHistory ? '修改关注重点,回车重新生成' : (phase === 'done' ? '如:重点看能否突破压力位…回车重新分析' : '可留空,留空则全面分析')}
							className={cn(
								'flex-1 h-8 px-3 rounded-lg bg-base ring-1 ring-border/30 text-xs text-foreground placeholder:text-muted/40',
								'focus:outline-none focus:ring-2 focus:ring-sky-400/30 transition-shadow disabled:opacity-50',
							)}
						/>
						{isHistory ? (
							<button
								onClick={handleStartNew}
								className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-gradient-to-r from-sky-500/20 to-blue-500/15 border border-sky-400/30 text-xs font-medium text-sky-300 hover:from-sky-500/30 hover:to-blue-500/20 transition-all shrink-0"
								title="以此关注点重新生成新报告"
							>
								<RefreshCw className="h-3.5 w-3.5" />重新生成
							</button>
						) : (
							<button
								onClick={handleStartNew}
								disabled={isWorking}
								className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-gradient-to-r from-sky-500/20 to-blue-500/15 border border-sky-400/30 text-xs font-medium text-sky-300 hover:from-sky-500/30 hover:to-blue-500/20 disabled:opacity-40 disabled:cursor-not-allowed transition-all shrink-0"
								title={focus.trim() ? '按关注重点重新分析' : '重新分析'}
							>
								{isWorking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : phase === 'done' ?
									<RefreshCw className="h-3.5 w-3.5" /> : <Send className="h-3.5 w-3.5" />}
								{phase === 'done' ? '重新分析' : '分析'}
							</button>
						)}
					</div>
					<p className="mt-1.5 text-[10px] text-muted/50 leading-relaxed">
						{isHistory
							? '历史报告为静态记录;修改关注重点后将作为新任务重新生成。报告仅供参考,不构成投资建议。'
							: '报告由项目已配置的 AI 模型基于本地行情与财务数据生成;消息面维度暂依据价量异动推断。报告仅供参考,不构成投资建议。'}
					</p>
				</div>
			</div>
		</div>
	)
}

function fmtRelative(iso: string): string {
	try {
		const t = new Date(iso).getTime()
		const diff = Date.now() - t
		if (diff < 60_000) return '刚刚'
		if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
		if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`
		if (diff < 7 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`
		return new Date(iso).toLocaleDateString('zh-CN')
	} catch {
		return ''
	}
}

/** P1-10 多周期战绩面板: T+1/3/5/10 命中率 + 平均涨跌/超额 + 最近明细; "不是回测"固定展示 */
function ScorecardPanel({ sc }: { sc: AiScorecard }) {
	const keys = Object.keys(sc.horizons).sort((a, b) => Number(a) - Number(b))
	const fmtPct = (v: number | null | undefined) => v == null ? '—' : `${v > 0 ? '+' : ''}${v}%`
	const pctCls = (v: number | null | undefined) => v == null ? 'text-muted' : v > 0 ? 'text-bull' : v < 0 ? 'text-bear' : 'text-muted'
	return (
		<div className="mt-1.5 rounded-lg border border-border/40 bg-elevated/20 px-3 py-2 text-xs">
			<table className="w-full">
				<thead>
					<tr className="text-[10px] text-muted">
						<th className="py-0.5 text-left font-normal">周期</th>
						<th className="py-0.5 text-right font-normal">命中率</th>
						<th className="py-0.5 text-right font-normal">结算数</th>
						<th className="py-0.5 text-right font-normal">平均涨跌</th>
						<th className="py-0.5 text-right font-normal" title={`相对 ${sc.benchmark.name} 的平均超额`}>平均超额</th>
					</tr>
				</thead>
				<tbody>
					{keys.map(k => {
						const h = sc.horizons[k]
						return (
							<tr key={k} className="border-t border-border/30">
								<td className="py-1 text-secondary">T+{k}</td>
								<td className="py-1 text-right">
									{h.insufficient || h.hit_rate_pct == null
										? <span className="text-[10px] text-muted">样本不足 {h.settled}/{sc.min_sample}</span>
										: <span className="font-semibold text-secondary">{h.hit_rate_pct}%</span>}
								</td>
								<td className="py-1 text-right text-muted">{h.settled}</td>
								<td className={`py-1 text-right ${pctCls(h.avg_pct)}`}>{fmtPct(h.avg_pct)}</td>
								<td className={`py-1 text-right ${pctCls(h.avg_alpha_pct)}`}
									title={h.alpha_samples < h.settled ? `${h.settled - h.alpha_samples} 条缺基准数据未计入` : undefined}>
									{fmtPct(h.avg_alpha_pct)}
								</td>
							</tr>
						)
					})}
				</tbody>
			</table>
			<p className="mt-1.5 border-t border-border/30 pt-1.5 text-[10px] leading-relaxed text-muted">
				⚠️ {sc.disclaimer}中性研判按涨跌幅在各周期容差带内算对 (T+1 ±1% … T+10 ±3%)。
			</p>
			{/* P1-15: 按模型分组战绩 — "哪个模型准"一目了然; 同款样本护栏 */}
			{sc.by_model && sc.by_model.length > 0 && (
				<div className="mt-1.5 border-t border-border/30 pt-1.5">
					<div className="mb-0.5 text-[10px] text-muted"
						title="按研判生成时的模型快照分组 (T+5 主周期口径); 「未标注」= 模型归属功能上线前的老战绩">
						按模型战绩 (T+{sc.horizon_days})
					</div>
					{sc.by_model.map(g => (
						<div key={g.model} className="flex items-center gap-2 py-0.5 text-[10px]">
							<span className="max-w-40 truncate text-secondary" title={g.model}>{g.model}</span>
							<span className="ml-auto shrink-0">
								{g.insufficient || g.hit_rate_pct == null
									? <span className="text-muted">样本不足 {g.settled}/{sc.min_sample}</span>
									: <span className="font-semibold text-secondary">{g.hit_rate_pct}%</span>}
							</span>
							<span className="w-10 shrink-0 text-right text-muted">{g.settled} 次</span>
						</div>
					))}
				</div>
			)}
			{sc.recent.length > 0 && (
				<div className="mt-1.5 border-t border-border/30 pt-1.5">
					<div className="mb-0.5 text-[10px] text-muted">最近结算 (最多 10 条)</div>
					{sc.recent.map((r, i) => (
						<div key={r.report_id ?? i} className="flex items-center gap-2 py-0.5 text-[10px]">
							<span className="shrink-0 text-muted">{r.analysis_date}</span>
							<span className="max-w-24 truncate text-secondary"
								title={r.model ? `模型: ${r.model}` : undefined}>{r.name || r.symbol}</span>
							<span className="shrink-0 text-muted">{r.direction}</span>
							<span className="ml-auto flex shrink-0 gap-1.5">
								{Object.entries(r.horizons ?? {}).sort((a, b) => Number(a[0]) - Number(b[0])).map(([k, hz]) => (
									<span key={k} className={hz.hit ? 'text-bull' : 'text-bear'}
										title={`T+${k} ${hz.hit ? '判对' : '判错'}${hz.alpha_pct != null ? `, 超额 ${hz.alpha_pct > 0 ? '+' : ''}${hz.alpha_pct}%` : ''}`}>
										{fmtPct(hz.pct)}
									</span>
								))}
								{r.expired && <span className="text-muted">已过期</span>}
							</span>
						</div>
					))}
				</div>
			)}
		</div>
	)
}
