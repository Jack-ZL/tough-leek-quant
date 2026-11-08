/**
 * AI 审计账本面板 (P2-23) — 不可篡改账本的倒查 UI。
 *
 * 三块能力:
 *  1. 链校验: 一键重算全链哈希 + 快照指纹复核, 篡改可发现;
 *  2. 条目倒查: run(输入指纹/产出哈希/模型归属) → link(报告绑定) → checkpoint(每日锚点);
 *  3. 单输入深挖: 查看当时快照原文 (未过期时) / 与当前数据源重算比对 (kline/financials)。
 */
import { useCallback, useEffect, useState } from 'react'
import {
	RefreshCw, ShieldCheck, ShieldAlert, ChevronDown, ChevronRight,
	FileSearch, GitCompareArrows, Eye,
} from 'lucide-react'
import {
	api,
	type AiAuditDetail, type AiAuditEntry, type AiAuditRecompareResult,
	type AiAuditVerifyResult,
} from '@/lib/api'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'

const KIND_LABELS: Record<string, string> = {
	stock_analysis: '个股分析',
	debate: '多空辩论',
	market_recap: '大盘复盘',
	financial: 'AI 财务',
}

const TYPE_STYLES: Record<string, string> = {
	run: 'bg-accent/10 text-accent border-accent/30',
	link: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/30',
	checkpoint: 'bg-muted text-muted-foreground border-border',
}

function shortHash(h?: string): string {
	return h ? `${h.slice(0, 8)}…${h.slice(-4)}` : '—'
}

export function SettingsAuditLedgerPanel() {
	// ===== 链校验 =====
	const [verifyResult, setVerifyResult] = useState<AiAuditVerifyResult | null>(null)
	const [verifying, setVerifying] = useState(false)

	const runVerify = useCallback(async () => {
		setVerifying(true)
		try {
			const r = await api.aiAuditVerify()
			setVerifyResult(r)
		} catch (e) {
			toast(`校验失败: ${e instanceof Error ? e.message : String(e)}`, 'error')
		} finally {
			setVerifying(false)
		}
	}, [])

	// ===== 条目列表 =====
	const [symbol, setSymbol] = useState('')
	const [kind, setKind] = useState('')
	const [limit, setLimit] = useState(50)
	const [entries, setEntries] = useState<AiAuditEntry[]>([])
	const [loading, setLoading] = useState(false)

	const loadEntries = useCallback(async () => {
		setLoading(true)
		try {
			const r = await api.aiAuditEntries({ symbol: symbol.trim(), kind, limit })
			setEntries(r.entries)
		} catch (e) {
			toast(`账本读取失败: ${e instanceof Error ? e.message : String(e)}`, 'error')
		} finally {
			setLoading(false)
		}
	}, [symbol, kind, limit])

	useEffect(() => { void loadEntries() }, [])  // 首屏一次; 之后手动/筛选变更触发

	// ===== run 详情展开 =====
	const [expanded, setExpanded] = useState<string | null>(null)
	const [detail, setDetail] = useState<AiAuditDetail | null>(null)
	const [detailLoading, setDetailLoading] = useState(false)

	const toggleExpand = useCallback(async (auditId: string) => {
		if (expanded === auditId) {
			setExpanded(null); setDetail(null)
			return
		}
		setExpanded(auditId); setDetail(null); setDetailLoading(true)
		try {
			setDetail(await api.aiAuditDetail(auditId))
		} catch (e) {
			toast(`详情读取失败: ${e instanceof Error ? e.message : String(e)}`, 'error')
		} finally {
			setDetailLoading(false)
		}
	}, [expanded])

	// ===== 快照查看 =====
	const [snapshot, setSnapshot] = useState<{ name: string; payload: unknown } | null>(null)
	const [snapshotLoading, setSnapshotLoading] = useState(false)

	const openSnapshot = useCallback(async (auditId: string, name: string) => {
		setSnapshotLoading(true)
		try {
			const r = await api.aiAuditSnapshot(auditId, name)
			setSnapshot({ name: r.name, payload: r.payload })
		} catch (e) {
			toast(`快照读取失败 (可能已过保留期): ${e instanceof Error ? e.message : String(e)}`, 'error')
		} finally {
			setSnapshotLoading(false)
		}
	}, [])

	// ===== 重算比对 =====
	const [recompare, setRecompare] = useState<AiAuditRecompareResult | null>(null)
	const [recompareLoading, setRecompareLoading] = useState(false)

	const runRecompare = useCallback(async (auditId: string, name: string) => {
		setRecompareLoading(true); setRecompare(null)
		try {
			setRecompare(await api.aiAuditRecompare(auditId, name))
		} catch (e) {
			toast(`比对失败: ${e instanceof Error ? e.message : String(e)}`, 'error')
		} finally {
			setRecompareLoading(false)
		}
	}, [])

	return (
		<div className="pb-8 flex flex-col gap-6">
			{/* ===== 链校验 ===== */}
			<section className="rounded-card border border-border bg-surface p-5">
				<div className="flex items-center justify-between gap-3 mb-3">
					<div className="flex items-center gap-2">
						<ShieldCheck className="h-4 w-4 text-accent" />
						<h3 className="text-sm font-medium text-foreground">账本链校验</h3>
					</div>
					<button
						type="button"
						onClick={() => void runVerify()}
						disabled={verifying}
						className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-muted disabled:opacity-50"
					>
						<RefreshCw className={`h-3.5 w-3.5 ${verifying ? 'animate-spin' : ''}`} />
						{verifying ? '校验中…' : '一键校验全链'}
					</button>
				</div>
				<p className="text-xs text-muted-foreground mb-3">
					账本只追加不改写: 每行带前一行哈希 (链式), 每份输入带 sha256 指纹 + 当时快照。
					任何人改动历史行或快照文件, 校验都会报出来。快照默认保留 {verifyResult?.retain_days ?? 90} 天, 过期只删文件、账本行不动。
				</p>
				{verifyResult && (
					<div className={`rounded-md border p-3 text-xs ${verifyResult.ok
						? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600'
						: 'border-red-500/30 bg-red-500/10 text-red-600'}`}>
						<div className="flex items-center gap-1.5 font-medium">
							{verifyResult.ok ? <ShieldCheck className="h-3.5 w-3.5" /> : <ShieldAlert className="h-3.5 w-3.5" />}
							{verifyResult.ok ? '链完整 — 未发现篡改' : `发现 ${verifyResult.errors.length} 处异常`}
							<span className="ml-auto font-normal opacity-80">
								校验 {verifyResult.checked} 行 · 快照复核 {verifyResult.snapshots_checked} 份 · 过期缺失 {verifyResult.snapshots_missing} 份
							</span>
						</div>
						{verifyResult.errors.length > 0 && (
							<ul className="mt-2 list-disc pl-5 space-y-0.5 break-all">
								{verifyResult.errors.map((e, i) => <li key={i}>{e}</li>)}
							</ul>
						)}
					</div>
				)}
			</section>

			{/* ===== 条目倒查 ===== */}
			<section className="rounded-card border border-border bg-surface p-5">
				<div className="flex items-center gap-2 mb-4">
					<FileSearch className="h-4 w-4 text-accent" />
					<h3 className="text-sm font-medium text-foreground">账本条目</h3>
				</div>
				<div className="flex flex-wrap items-center gap-2 mb-4">
					<input
						value={symbol}
						onChange={e => setSymbol(e.target.value)}
						placeholder="标的代码 (复盘类为日期)"
						className="rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-foreground placeholder:text-muted-foreground w-48"
					/>
					<select
						value={kind}
						onChange={e => setKind(e.target.value)}
						className="rounded-md border border-border bg-background px-2 py-1.5 text-xs text-foreground"
					>
						<option value="">全部类型</option>
						{Object.entries(KIND_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
					</select>
					<select
						value={limit}
						onChange={e => setLimit(Number(e.target.value))}
						className="rounded-md border border-border bg-background px-2 py-1.5 text-xs text-foreground"
					>
						<option value={50}>最近 50 条</option>
						<option value={200}>最近 200 条</option>
						<option value={500}>最近 500 条</option>
					</select>
					<button
						type="button"
						onClick={() => void loadEntries()}
						disabled={loading}
						className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-muted disabled:opacity-50"
					>
						<RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
						查询
					</button>
				</div>

				{entries.length === 0 && !loading && (
					<p className="text-xs text-muted-foreground py-6 text-center">
						暂无账本条目 — 完成一次 AI 分析 (个股/辩论/复盘/财务) 后自动记账
					</p>
				)}
				<div className="flex flex-col gap-1.5">
					{entries.map(e => (
						<div key={e.seq} className="rounded-md border border-border bg-background">
							{/* 行头 */}
							<div
								className={`flex items-center gap-2 px-3 py-2 text-xs ${e.type === 'run' ? 'cursor-pointer hover:bg-muted/50' : ''}`}
								onClick={() => { if (e.type === 'run' && e.audit_id) void toggleExpand(e.audit_id) }}
							>
								{e.type === 'run'
									? (expanded === e.audit_id
										? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
										: <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />)
									: <span className="w-3.5 shrink-0" />}
								<span className="text-muted-foreground font-mono">#{e.seq}</span>
								<span className={`shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] ${TYPE_STYLES[e.type] ?? TYPE_STYLES.checkpoint}`}>
									{e.type === 'run' ? (KIND_LABELS[e.kind ?? ''] ?? e.kind ?? 'run') : e.type}
								</span>
								{e.symbol && <span className="font-medium text-foreground">{e.symbol}</span>}
								{e.type === 'link' && (
									<span className="text-muted-foreground">→ 报告 {e.report_id}</span>
								)}
								{e.type === 'checkpoint' && (
									<span className="text-muted-foreground">{e.day} · 当日 {e.runs_today} 次分析</span>
								)}
								{e.type === 'run' && e.model && (
									<span className="text-muted-foreground truncate">{e.model}</span>
								)}
								<span className="ml-auto shrink-0 text-muted-foreground">{e.ts}</span>
							</div>

							{/* run 展开详情 */}
							{e.type === 'run' && expanded === e.audit_id && (
								<div className="border-t border-border px-3 py-3 text-xs">
									{detailLoading && <p className="text-muted-foreground">加载中…</p>}
									{detail && detail.audit_id === e.audit_id && (
										<div className="flex flex-col gap-3">
											{/* 归属 + 哈希 */}
											<div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-muted-foreground">
												<div>模型: <span className="text-foreground">{detail.model || '—'}</span></div>
												<div>渠道: <span className="text-foreground">{detail.provider || '—'}</span></div>
												<div className="col-span-2">prompt 哈希: <span className="font-mono text-foreground">{shortHash(detail.prompt_sha256)}</span></div>
												<div className="col-span-2">产出哈希: <span className="font-mono text-foreground">{shortHash(detail.output_sha256)}</span></div>
												<div>产出字数: <span className="text-foreground">{detail.output_chars ?? '—'}</span></div>
												<div>audit_id: <span className="font-mono text-foreground">{detail.audit_id?.slice(0, 12)}…</span></div>
											</div>
											{/* 输入指纹表 */}
											<div>
												<p className="font-medium text-foreground mb-1.5">输入数据指纹 ({detail.inputs?.length ?? 0} 份)</p>
												<table className="w-full text-left">
													<thead>
														<tr className="text-muted-foreground border-b border-border">
															<th className="py-1 pr-2 font-normal">名称</th>
															<th className="py-1 pr-2 font-normal">数据日期</th>
															<th className="py-1 pr-2 font-normal">行数</th>
															<th className="py-1 pr-2 font-normal">sha256</th>
															<th className="py-1 pr-2 font-normal">快照</th>
															<th className="py-1 font-normal">操作</th>
														</tr>
													</thead>
													<tbody>
														{(detail.inputs ?? []).map(inp => (
															<tr key={inp.name} className="border-b border-border/50">
																<td className="py-1.5 pr-2 font-mono text-foreground">{inp.name}</td>
																<td className="py-1.5 pr-2 text-muted-foreground">{inp.as_of || '—'}</td>
																<td className="py-1.5 pr-2 text-muted-foreground">{inp.rows ?? '—'}</td>
																<td className="py-1.5 pr-2 font-mono text-muted-foreground" title={inp.sha256}>{shortHash(inp.sha256)}</td>
																<td className="py-1.5 pr-2">
																	{inp.snapshot_state === 'ok'
																		? <span className="text-emerald-600">在</span>
																		: <span className="text-muted-foreground">已过期</span>}
																</td>
																<td className="py-1.5">
																	<div className="flex items-center gap-1.5">
																		{inp.snapshot_state === 'ok' && (
																			<button
																				type="button"
																				disabled={snapshotLoading}
																				onClick={() => void openSnapshot(detail.audit_id ?? '', inp.name)}
																				className="flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[10px] text-foreground hover:bg-muted disabled:opacity-50"
																			>
																				<Eye className="h-3 w-3" />快照
																			</button>
																		)}
																		{(inp.name === 'kline' || inp.name === 'financials') && (
																			<button
																				type="button"
																				disabled={recompareLoading}
																				onClick={() => void runRecompare(detail.audit_id ?? '', inp.name)}
																				className="flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[10px] text-foreground hover:bg-muted disabled:opacity-50"
																			>
																				<GitCompareArrows className="h-3 w-3" />重算比对
																			</button>
																		)}
																	</div>
																</td>
															</tr>
														))}
													</tbody>
												</table>
												{/* 重算比对结果 */}
												{recompare && (
													<div className={`mt-2 rounded-md border p-2 text-[11px] ${recompare.match === true
														? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600'
														: recompare.match === false
															? 'border-amber-500/30 bg-amber-500/10 text-amber-600'
															: 'border-border bg-muted/50 text-muted-foreground'}`}>
														{recompare.recomparable === false && (recompare.note ?? '该输入不做当前重算比对')}
														{recompare.recomparable && recompare.current_available === false && '当前数据源无此数据, 无法比对 (当时指纹仍在)'}
														{recompare.match === true && '指纹一致 — 当时的输入数据与当前数据源完全相同'}
														{recompare.match === false && (
															<span>
																指纹不一致 — 数据源在分析后发生过变化 (当时 {shortHash(recompare.stored_sha256)} → 现在 {shortHash(recompare.current_sha256)})
															</span>
														)}
													</div>
												)}
											</div>
											{/* 报告绑定 */}
											<div>
												<p className="font-medium text-foreground mb-1">报告绑定 ({detail.links.length})</p>
												{detail.links.length === 0
													? <p className="text-muted-foreground">未绑定 — 该次分析的产出尚未保存为报告</p>
													: detail.links.map(l => (
														<p key={l.seq} className="text-muted-foreground">
															<span className="font-mono">#{l.seq}</span> → 报告 <span className="font-mono text-foreground">{l.report_id}</span>
															{l.kind ? ` (${KIND_LABELS[l.kind] ?? l.kind})` : ''} · {l.ts}
														</p>
													))}
											</div>
										</div>
									)}
								</div>
							)}
						</div>
					))}
				</div>
			</section>

			{/* ===== 快照原文弹窗 ===== */}
			{snapshot && (
				<Modal onClose={() => setSnapshot(null)} ariaLabel={`快照 ${snapshot.name}`}
					panelClassName="w-[92vw] max-w-3xl bg-surface border border-border rounded-card shadow-xl flex flex-col max-h-[80vh]">
					<div className="flex items-center justify-between px-4 py-3 border-b border-border">
						<h3 className="text-sm font-medium text-foreground">当时快照 — {snapshot.name}</h3>
						<button type="button" onClick={() => setSnapshot(null)}
							className="text-xs text-muted-foreground hover:text-foreground">关闭</button>
					</div>
					<pre className="flex-1 overflow-auto p-4 text-[11px] leading-relaxed font-mono text-foreground whitespace-pre-wrap break-all">
						{JSON.stringify(snapshot.payload, null, 2)}
					</pre>
				</Modal>
			)}
		</div>
	)
}
