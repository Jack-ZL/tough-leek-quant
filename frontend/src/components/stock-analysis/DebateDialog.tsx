import { useEffect, useRef } from 'react'
import {
	X, Swords, Scale, History, Trash2, ArrowLeft, Loader2, Sparkles, AlertTriangle,
} from 'lucide-react'
import { cn } from '@/lib/cn'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { verdictTone } from '@/lib/stockAnalysisStore'
// P1-11 数据体检徽章 (与个股分析对话框共用)
import { QualityBadge } from './QualityBadge'
import type { AiVerdict, DebateRole } from '@/lib/api'
import {
	useDebateState, closeDebate, startDebate, toggleHistory, viewReport, backToLive,
	deleteHistoryReport, loadHistory,
} from '@/lib/debateStore'

/**
 * 多空辩论对话框 (P1-9) —— 双栏对抗布局: 多方(红) | 空方(绿), 底部风控主管(琥珀)。
 *
 * A 股配色习惯: 红=多, 绿=空 (与 K 线一致)。
 * 流式期间三方文本按 role_delta 事件路由到各自栏, 主管裁决落定后顶部出裁决徽标。
 * 历史面板复用同一布局渲染落库记录。
 */

const ROLE_META: Record<DebateRole, { label: string; icon: typeof Swords }> = {
	bull: { label: '多方辩手', icon: Swords },
	bear: { label: '空方辩手', icon: Swords },
	judge: { label: '风控主管', icon: Scale },
}

/** 各栏主题 (边框/标题/光标点) */
const ROLE_THEME: Record<DebateRole, { head: string; dot: string; badge: string }> = {
	bull: {
		head: 'from-rose-500/[0.08] to-transparent border-rose-400/20',
		dot: 'bg-rose-400', badge: 'text-rose-300 border-rose-400/30 bg-rose-500/10',
	},
	bear: {
		head: 'from-emerald-500/[0.08] to-transparent border-emerald-400/20',
		dot: 'bg-emerald-400', badge: 'text-emerald-300 border-emerald-400/30 bg-emerald-500/10',
	},
	judge: {
		head: 'from-amber-500/[0.08] to-transparent border-amber-400/20',
		dot: 'bg-amber-400', badge: 'text-amber-300 border-amber-400/30 bg-amber-500/10',
	},
}

function fmtRelative(iso: string): string {
	try {
		const diff = Date.now() - new Date(iso).getTime()
		if (diff < 60_000) return '刚刚'
		if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
		if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`
		if (diff < 7 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`
		return new Date(iso).toLocaleDateString('zh-CN')
	} catch {
		return ''
	}
}

/** 裁决徽标: 方向 + 一句话理由; ok=false 明示"未交裁决" (fail-honest, 不猜) */
function VerdictBadge({ verdict, ok, final }: { verdict?: AiVerdict | null; ok?: boolean; final?: boolean }) {
	if (ok === false) {
		return (
			<span className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full border border-border/50 text-muted">
				<AlertTriangle className="h-3 w-3" />AI 未按格式交裁决{final ? '' : ' (初审)'}
			</span>
		)
	}
	if (!verdict?.direction) return null
	const tone = verdictTone(verdict.direction)
	return (
		<span
			title={verdict.reason}
			className={cn('inline-flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-full border',
				tone === 'bull' && 'border-rose-400/40 bg-rose-500/10 text-rose-300',
				tone === 'bear' && 'border-emerald-400/40 bg-emerald-500/10 text-emerald-300',
				tone === 'neutral' && 'border-border/50 bg-elevated text-secondary')}
		>
			<Scale className="h-3 w-3 shrink-0" />
			{final ? '终审' : '裁决'}: {verdict.direction}
			{verdict.reason && <span className="opacity-70 truncate max-w-[240px]">· {verdict.reason}</span>}
		</span>
	)
}

/** 单方辩论栏 (多方/空方共用; 主管栏全宽时也用) */
function RoleColumn({
	role, text, streaming, rewritten, rewriteReason, model, wide,
}: {
	role: DebateRole
	text: string
	streaming: boolean
	rewritten?: boolean
	rewriteReason?: string
	model?: string
	wide?: boolean
}) {
	const theme = ROLE_THEME[role]
	const meta = ROLE_META[role]
	const Icon = meta.icon
	return (
		<div className={cn('flex flex-col min-h-0 rounded-xl border border-border/40 bg-surface/60 overflow-hidden',
			wide ? 'flex-1' : 'flex-1 min-w-0')}>
			<div className={cn('px-3 py-2 border-b bg-gradient-to-r flex items-center gap-2 shrink-0', theme.head)}>
				<Icon className={cn('h-3.5 w-3.5',
					role === 'bull' && 'text-rose-300', role === 'bear' && 'text-emerald-300', role === 'judge' && 'text-amber-300')} />
				<span className="text-xs font-semibold text-foreground">{meta.label}</span>
				{model && <span className="text-[10px] text-muted font-mono truncate">{model}</span>}
				{rewritten && (
					<span className={cn('text-[10px] px-1.5 py-px rounded-full border', theme.badge)} title={rewriteReason || undefined}>
						已重写
					</span>
				)}
				{streaming && <span className={cn('h-1.5 w-1.5 rounded-full animate-pulse ml-auto shrink-0', theme.dot)} />}
			</div>
			<div className="flex-1 overflow-y-auto px-3 py-2 text-xs leading-relaxed min-h-[120px]">
				{rewriteReason && rewritten && (
					<div className="mb-2 text-[10px] text-amber-300/80 bg-amber-500/[0.06] border border-amber-400/20 rounded-lg px-2 py-1">
						主管打回: {rewriteReason}
					</div>
				)}
				{text
					? <MarkdownRenderer content={text} />
					: streaming
						? <span className="text-muted">思考中…</span>
						: <span className="text-muted">—</span>}
				{streaming && text &&
					<span className="inline-block w-1.5 h-3.5 ml-0.5 align-text-bottom bg-current animate-pulse rounded-sm" />}
			</div>
		</div>
	)
}

/** 历史记录面板 (列表 + 单条查看) */
function HistoryPanel() {
	const { history, viewing } = useDebateState()
	if (viewing) {
		return (
			<div className="flex flex-col gap-3 min-h-0">
				<div className="flex items-center gap-2 shrink-0">
					<button onClick={backToLive}
						className="flex items-center gap-1 text-[11px] text-muted hover:text-foreground transition-colors">
						<ArrowLeft className="h-3 w-3" />返回列表
					</button>
					<span className="text-xs text-secondary">{viewing.name}
						<span className="font-mono text-[10px] text-muted">{viewing.symbol}</span></span>
					<span className="text-[10px] text-muted">{fmtRelative(viewing.created_at)}</span>
					<VerdictBadge verdict={viewing.verdict} ok={viewing.verdict ? true : undefined} final />
				</div>
				<div className="flex gap-3 min-h-0 flex-1">
					<RoleColumn role="bull" text={viewing.bull_text} streaming={false} model={viewing.roles?.bull} />
					<RoleColumn role="bear" text={viewing.bear_text} streaming={false} model={viewing.roles?.bear} />
				</div>
				<RoleColumn role="judge" text={viewing.judge_text} streaming={false} wide
					model={viewing.roles?.judge} rewritten={viewing.rewritten} />
			</div>
		)
	}
	return (
		<div className="flex flex-col gap-1.5 min-h-0 overflow-y-auto">
			{history.length === 0 && <div className="text-xs text-muted py-8 text-center">暂无辩论记录</div>}
			{history.map(r => (
				<div key={r.id}
					className="flex items-center gap-2 px-3 py-2 rounded-lg border border-border/40 hover:bg-elevated/60 transition-colors group">
					<button className="flex-1 min-w-0 flex items-center gap-2 text-left" onClick={() => viewReport(r)}>
						<span className="text-xs text-foreground font-medium shrink-0">{r.name || r.symbol}</span>
						<span className="text-[10px] font-mono text-muted shrink-0">{r.symbol}</span>
						{r.verdict?.direction && <VerdictBadge verdict={r.verdict} final />}
						{r.rewritten && <span className="text-[10px] text-amber-300/80 shrink-0">经重写</span>}
						<span className="text-[10px] text-muted ml-auto shrink-0">{fmtRelative(r.created_at)}</span>
					</button>
					<button onClick={() => void deleteHistoryReport(r.id)} title="删除记录"
						className="p-1 rounded text-muted hover:text-rose-300 opacity-0 group-hover:opacity-100 transition-all shrink-0">
						<Trash2 className="h-3.5 w-3.5" />
					</button>
				</div>
			))}
		</div>
	)
}

export function DebateDialog() {
	const s = useDebateState()
	const scrollRef = useRef<HTMLDivElement>(null)
	const backdrop = useDialogBackdrop(closeDebate, () => s.phase !== 'streaming')

	// 打开时拉一次历史 (静默)
	useEffect(() => {
		if (s.open && s.history.length === 0) void loadHistory()
	}, [s.open]) // eslint-disable-line react-hooks/exhaustive-deps

	// 流式期间自动滚到底 (用户手动上滚后不强拉 — 简单处理: 只在接近底部时跟随)
	useEffect(() => {
		const el = scrollRef.current
		if (!el || s.viewing || s.phase === null) return
		if (el.scrollHeight - el.scrollTop - el.clientHeight < 200) {
			el.scrollTop = el.scrollHeight
		}
	}, [s.texts, s.viewing])

	if (!s.open) return null

	const isWorking = s.phase === 'streaming'
	// 空闲 (未开辩) 时永远落地在历史列表 — ⚔️ 按钮零成本看历史, 想辩再点「发起新辩论」
	const showHistory = s.historyOpen || s.phase === null
	// 生效裁决: 终审优先 (ruling 事件里 final=true 的会覆盖)
	const ruling = s.ruling

	return (
		<div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 backdrop-blur-sm p-4" {...backdrop}>
			<div className="dialog-pop w-full max-w-5xl max-h-[90vh] bg-surface/95 border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden">
				{/* 头部 */}
				<div className="relative px-5 py-3.5 border-b border-border/50 bg-gradient-to-r from-orange-500/[0.06] via-rose-500/[0.04] to-transparent shrink-0">
					<div className="flex items-center gap-3">
						<div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500/20 to-rose-500/15 border border-orange-400/30 shrink-0">
							<Swords className="h-4.5 w-4.5 text-orange-300" />
						</div>
						<div className="min-w-0 flex-1">
							<div className="flex items-center gap-2">
								<span className="text-sm font-semibold text-foreground">多空辩论</span>
								<span className="text-xs text-secondary truncate">{s.name}</span>
								<span className="text-[10px] font-mono text-muted shrink-0">{s.symbol}</span>
								{ruling && !s.historyOpen && !s.viewing &&
									<VerdictBadge verdict={ruling.verdict} ok={ruling.ok} final={ruling.final} />}
							</div>
							<div className="flex items-center gap-2 mt-0.5 text-[10px] text-muted">
								{s.meta?.summary ? (
									<span className="flex items-center gap-1 truncate">
										<Sparkles className="h-2.5 w-2.5 shrink-0" />
										<span className="truncate">{s.meta.summary}</span>
									</span>
								) : isWorking ? <span>正在准备材料…</span>
									: s.phase === null ? <span>查看历史辩论, 或发起一场新辩论</span> : null}
								{isWorking && (
									<span className="flex items-center gap-1 text-orange-300 shrink-0">
										<Loader2 className="h-2.5 w-2.5 animate-spin" />辩论中
									</span>
								)}
							</div>
						</div>
						<div className="flex items-center gap-1 shrink-0">
							{s.phase !== null && (
								<button onClick={toggleHistory} title="历史辩论记录"
									className={cn('p-1.5 rounded-lg hover:bg-elevated transition-colors',
										showHistory ? 'text-orange-300' : 'text-muted hover:text-foreground')}>
									<History className="h-4 w-4" />
								</button>
							)}
							<button onClick={closeDebate} title={isWorking ? '关闭 (后台继续, 重开可见进度)' : '关闭'}
								className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
								<X className="h-4 w-4" />
							</button>
						</div>
					</div>
				</div>

				{/* 内容区 */}
				<div ref={scrollRef} className="flex-1 overflow-y-auto px-5 py-4 min-h-[320px]">
					{showHistory ? (
						<div className="flex flex-col gap-3">
							{!s.viewing && (
								<div className="flex items-center gap-2 shrink-0">
									<button onClick={startDebate}
										className="h-8 px-4 rounded-lg bg-accent text-white text-xs font-medium flex items-center gap-1.5 hover:bg-accent/90 transition-all active:scale-[0.98]">
										<Swords className="h-3.5 w-3.5" />发起新辩论{s.name ? ` · ${s.name}` : ''}
									</button>
									<span className="text-[10px] text-muted">一场辩论 3~5 次模型调用 (多/空/主管, 至多打回重写一轮)</span>
								</div>
							)}
							<HistoryPanel />
						</div>
					) : (
						<div className="flex flex-col gap-3">
							{/* P1-11 数据体检徽章: 备料时对同一份材料打分, 多方/空方/主管看到的一致 */}
							{s.meta?.quality && <QualityBadge quality={s.meta.quality} />}
							{/* 双栏对抗: 多方(红) | 空方(绿) */}
							<div className="flex gap-3 items-stretch min-h-[200px]">
								<RoleColumn role="bull" text={s.texts.bull} streaming={s.streaming.bull}
									rewritten={s.rewritten.bull} rewriteReason={s.rewriteReason}
									model={s.roles.bull?.model} />
								<RoleColumn role="bear" text={s.texts.bear} streaming={s.streaming.bear}
									rewritten={s.rewritten.bear} rewriteReason={s.rewriteReason}
									model={s.roles.bear?.model} />
							</div>
							{/* 风控主管 (全宽) */}
							<RoleColumn role="judge" text={s.texts.judge} streaming={s.streaming.judge} wide
								model={s.roles.judge?.model} />
							{/* 打回提示 (初审 rewrite 时, 裁决徽标之外给一行说明) */}
							{ruling?.decision === 'rewrite' && !ruling.final && (
								<div className="text-[11px] text-amber-300/90 bg-amber-500/[0.06] border border-amber-400/20 rounded-lg px-3 py-1.5">
									主管认为{s.rewriteReason ? `: ${s.rewriteReason}` : '有一方论证不合格, 已打回重写 (至多一轮)'}
								</div>
							)}
							{s.phase === 'error' && (
								<div className="flex items-center gap-2 text-xs text-rose-300 bg-rose-500/[0.06] border border-rose-400/20 rounded-lg px-3 py-2">
									<AlertTriangle className="h-3.5 w-3.5 shrink-0" />{s.error}
								</div>
							)}
						</div>
					)}
				</div>
			</div>
		</div>
	)
}
