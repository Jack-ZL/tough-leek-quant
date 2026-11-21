import {useMemo, useState, type ReactNode} from 'react'
import {useQuery} from '@tanstack/react-query'
import {AlertTriangle, CalendarRange, HandCoins, RefreshCw, Search} from 'lucide-react'
import {api, ApiError, type HkDividendEvent} from '@/lib/api'
import {QK} from '@/lib/queryKeys'
import {PageHeader} from '@/components/PageHeader'
import {Skeleton} from '@/components/data/Skeleton'

/**
 * 港股分红日历 (PR7)。按**除净日**窗口列出全市场公司行动。
 *
 * 🔴 三条口径约束, 改这个文件前先读 `HkDividendEvent` 的注释:
 * 1. `dividend_yield_pct` 已经是**百分比数值**(5.0 = 5.0%), 这里只拼 `%`,
 *    **绝不再乘 100**(§3.1 禁止前端做单位启发式转换)。
 * 2. `null` 与 `0` 是两种状态: 算不出股息率 → 渲染「—」; 纯送转 → 渲染 `0.00%`。
 *    把 null 显示成 0 等于把「未同步/停牌」伪装成「零股息」。
 * 3. 上游失败(502)必须显示成**故障**, 不能落到「暂无数据」的空态里 ——
 *    「拉不到」和「这段时间没人除净」是两件事(§4 fail closed)。
 *
 * 数据**不落库**: 每次 query miss 都会打一次东财报表, 故 staleTime 给到 10 分钟,
 * 并且不提供「自动轮询」。
 */

/** 数据不落库 ⇒ 每次 miss 都打上游。10 分钟内复用缓存, 不自动刷新。 */
const STALE_MS = 600_000

/** 与后端 `HK_DIVIDEND_MAX_WINDOW_DAYS` 对齐; 超了后端直接 400。 */
const MAX_WINDOW_DAYS = 400

const PRESETS = [
	{label: '未来 30 天', startOffset: 0, endOffset: 30},
	{label: '未来 60 天', startOffset: 0, endOffset: 60},
	{label: '未来 90 天', startOffset: 0, endOffset: 90},
	{label: '近 30 天', startOffset: -30, endOffset: 0},
] as const

type SortKey = 'ex_date' | 'symbol' | 'dividend_hkd' | 'dividend_yield_pct'

function toIso(d: Date): string {
	// 用本地年月日拼, 不用 toISOString(): 后者是 UTC, 东八区早上会差一天
	const p = (n: number) => String(n).padStart(2, '0')
	return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

function shiftIso(base: string, days: number): string {
	const d = new Date(`${base}T00:00:00`)
	d.setDate(d.getDate() + days)
	return toIso(d)
}

function windowDays(start: string, end: string): number {
	return Math.round(
		(new Date(`${end}T00:00:00`).getTime() - new Date(`${start}T00:00:00`).getTime()) / 86_400_000,
	)
}

/** 空值占位。🔴 不能换成 `0` / `0.00%`: 那会把「算不出来」说成「没有」。 */
const NA = '—'

/**
 * null 永远沉底的比较器。
 *
 * 🔴 与方向无关: 降序时也不能让 null 冒到最前面(后端 polars 的
 * `sort(descending=True)` 默认就是 nulls first, 同一个坑在前端会再犯一次 ——
 * 「股息率从高到低」排出来第一屏全是「—」, 看着像数据全坏了)。
 */
function cmpNullLast<T extends string | number>(a: T | null, b: T | null, dir: 1 | -1): number {
	if (a == null && b == null) return 0
	if (a == null) return 1
	if (b == null) return -1
	return a < b ? -dir : a > b ? dir : 0
}

export function HkDividends() {
	const today = useMemo(() => toIso(new Date()), [])
	const [start, setStart] = useState(() => today)
	const [end, setEnd] = useState(() => shiftIso(today, 60))
	const [sortKey, setSortKey] = useState<SortKey>('ex_date')
	const [sortDir, setSortDir] = useState<1 | -1>(1)
	const [q, setQ] = useState('')
	const [cashOnly, setCashOnly] = useState(false)

	const days = windowDays(start, end)
	const invalid = days < 0 || days > MAX_WINDOW_DAYS
	// 非法窗口不打请求: 后端会 400, 但让用户先看到原因比吃一个报错更好
	const query = useQuery({
		queryKey: QK.hkDividends(start, end),
		queryFn: () => api.hkDividends({start, end}),
		staleTime: STALE_MS,
		enabled: !invalid,
		retry: false,
	})

	const items = query.data?.items ?? []
	const rows = useMemo(() => {
		const kw = q.trim().toLowerCase()
		const filtered = items.filter(it => {
			if (cashOnly && !(it.dividend_hkd != null && it.dividend_hkd > 0)) return false
			if (!kw) return true
			return (
				it.symbol.toLowerCase().includes(kw) ||
				(it.name ?? '').toLowerCase().includes(kw) ||
				(it.plan ?? '').toLowerCase().includes(kw)
			)
		})
		return [...filtered].sort((a, b) => {
			const c = cmpNullLast(a[sortKey], b[sortKey], sortDir)
			// 主键同值(尤其是一堆 null)时按 除净日+代码 兜底, 保证顺序稳定可复现
			return c !== 0 ? c : cmpNullLast(a.ex_date, b.ex_date, 1) || a.symbol.localeCompare(b.symbol)
		})
	}, [items, q, cashOnly, sortKey, sortDir])

	const stats = useMemo(() => {
		const yields = items.map(i => i.dividend_yield_pct).filter((v): v is number => v != null)
		return {
			symbols: new Set(items.map(i => i.symbol)).size,
			maxYield: yields.length ? Math.max(...yields) : null,
		}
	}, [items])

	const toggleSort = (key: SortKey) => {
		if (key === sortKey) setSortDir(d => (d === 1 ? -1 : 1))
		else {
			setSortKey(key)
			// 金额/股息率天然「大的先看」, 日期与代码天然「早的/小的先看」
			setSortDir(key === 'dividend_hkd' || key === 'dividend_yield_pct' ? -1 : 1)
		}
	}

	const applyPreset = (p: (typeof PRESETS)[number]) => {
		setStart(shiftIso(today, p.startOffset))
		setEnd(shiftIso(today, p.endOffset))
	}

	const upstreamDown = query.error instanceof ApiError && query.error.status === 502

	return (
		<div className="flex h-full flex-col">
			<PageHeader
				title="港股分红日历"
				subtitle="按除净日排列的全市场公司行动 · 实时取自东财报表(不落库) · 金额与股息率均为港币口径"
				right={
					<button
						type="button"
						onClick={() => void query.refetch()}
						disabled={query.isFetching || invalid}
						title="重新向上游取数"
						className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border bg-surface/80 px-3 text-xs text-secondary transition-colors hover:text-foreground disabled:opacity-50"
					>
						<RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? 'animate-spin' : ''}`} />
						刷新
					</button>
				}
			/>
			<div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
				<div className="mx-auto max-w-7xl space-y-4">
					{/* 窗口选择 */}
					<section className="rounded-card border border-border bg-surface p-3">
						<div className="flex flex-wrap items-center gap-2">
							<CalendarRange className="h-4 w-4 shrink-0 text-muted" />
							{PRESETS.map(p => {
								const active = start === shiftIso(today, p.startOffset) && end === shiftIso(today, p.endOffset)
								return (
									<button
										key={p.label}
										type="button"
										onClick={() => applyPreset(p)}
										className={`h-7 rounded-btn border px-2.5 text-[11px] transition-colors ${
											active
												? 'border-accent/40 bg-accent/10 font-medium text-accent'
												: 'border-border bg-base text-secondary hover:text-foreground'
										}`}
									>
										{p.label}
									</button>
								)
							})}
							<span className="mx-1 h-4 w-px bg-border" />
							<input
								type="date"
								value={start}
								max={end}
								onChange={e => e.target.value && setStart(e.target.value)}
								className="h-7 rounded-btn border border-border bg-base px-2 font-mono text-[11px] text-foreground"
								aria-label="起始除净日"
							/>
							<span className="text-[11px] text-muted">至</span>
							<input
								type="date"
								value={end}
								min={start}
								onChange={e => e.target.value && setEnd(e.target.value)}
								className="h-7 rounded-btn border border-border bg-base px-2 font-mono text-[11px] text-foreground"
								aria-label="结束除净日"
							/>
							<span className={`text-[11px] ${invalid ? 'text-danger' : 'text-muted'}`}>
								共 {days} 天{invalid ? `(上限 ${MAX_WINDOW_DAYS} 天, 且起始不得晚于结束)` : ''}
							</span>
						</div>
					</section>

					{invalid ? (
						<div className="rounded-card border border-dashed border-danger/40 px-5 py-10 text-center text-sm text-danger">
							窗口不合法, 请调整日期后重试。
						</div>
					) : upstreamDown ? (
						/* 🔴 独立于空态: 上游挂了必须说「挂了」, 不能说「没有分红」 */
						<div className="rounded-card border border-danger/40 bg-danger/5 px-5 py-10 text-center">
							<AlertTriangle className="mx-auto h-6 w-6 text-danger" />
							<p className="mt-2 text-sm font-medium text-danger">港股分红数据源不可用</p>
							<p className="mt-1 text-xs text-secondary">{(query.error as Error).message}</p>
							<p className="mt-2 text-[11px] text-muted">
								这不是「该窗口内没有除净」, 而是上游报表取数失败。稍后重试, 或到 数据 页检查东财凭据与限流状态。
							</p>
						</div>
					) : query.error ? (
						<div className="rounded-card border border-danger/40 bg-danger/5 px-5 py-10 text-center text-sm text-danger">
							加载失败 · {(query.error as Error).message}
						</div>
					) : (
						<>
							{/* 概览 + 数据质量 */}
							<section className="flex flex-wrap items-center gap-2">
								<Stat label="事件" value={String(query.data?.total ?? 0)} />
								<Stat label="标的" value={String(stats.symbols)} />
								<Stat
									label="最高股息率"
									value={stats.maxYield == null ? NA : `${stats.maxYield.toFixed(2)}%`}
								/>
								<Stat label="窗口" value={`${query.data?.start ?? ''} ~ ${query.data?.end ?? ''}`} mono />
								<div className="ml-auto flex items-center gap-2">
									<QualityFlag
										count={query.data?.unparsed_count ?? 0}
										label="方案未解析"
										hint="源数据没给港币折算值(外币未折算/分派股份/权证)。这些行仍在下表里, 只是每股派息与股息率显示为「—」, 方案原文照实展示。"
									/>
									<QualityFlag
										count={query.data?.missing_close_count ?? 0}
										label="缺收盘价"
										hint="最近若干个日K分区里取不到该标的收盘(未同步/长期停牌/人民币柜台不在日K里), 因此算不出股息率。显示为「—」而不是 0。"
									/>
								</div>
							</section>

							{/* 过滤条 */}
							<section className="flex flex-wrap items-center gap-2">
								<div className="relative">
									<Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
									<input
										value={q}
										onChange={e => setQ(e.target.value)}
										placeholder="代码 / 名称 / 方案"
										className="h-8 w-56 rounded-btn border border-border bg-surface pl-7 pr-2 text-xs text-foreground placeholder:text-muted"
									/>
								</div>
								<label className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-btn border border-border bg-surface px-2.5 text-[11px] text-secondary">
									<input
										type="checkbox"
										checked={cashOnly}
										onChange={e => setCashOnly(e.target.checked)}
										className="h-3 w-3 accent-current"
									/>
									只看有现金派息
								</label>
								<span className="text-[11px] text-muted">
									显示 {rows.length} / {items.length} 行
								</span>
							</section>

							{/* 日历表 */}
							<section className="overflow-hidden rounded-card border border-border bg-surface">
								<table className="w-full text-xs">
									<thead className="border-b border-border bg-base/60 text-[11px] text-muted">
										<tr>
											<Th onClick={() => toggleSort('ex_date')} active={sortKey === 'ex_date'} dir={sortDir}>
												除净日
											</Th>
											<Th onClick={() => toggleSort('symbol')} active={sortKey === 'symbol'} dir={sortDir}>
												代码
											</Th>
											<th className="px-2 py-2 text-left font-normal">名称</th>
											<th className="px-2 py-2 text-left font-normal">行业</th>
											<th className="px-2 py-2 text-left font-normal">方案</th>
											<th className="px-2 py-2 text-left font-normal">类型</th>
											<Th
												onClick={() => toggleSort('dividend_hkd')}
												active={sortKey === 'dividend_hkd'}
												dir={sortDir}
												right
											>
												每股派息
											</Th>
											<th className="px-2 py-2 text-right font-normal">送转</th>
											<Th
												onClick={() => toggleSort('dividend_yield_pct')}
												active={sortKey === 'dividend_yield_pct'}
												dir={sortDir}
												right
											>
												股息率
											</Th>
											<th className="px-2 py-2 text-left font-normal">派息日</th>
											<th className="px-2 py-2 text-left font-normal">收盘</th>
										</tr>
									</thead>
									<tbody>
										{rows.map(it => (
											<Row key={`${it.symbol}-${it.ex_date}-${it.plan ?? ''}`} it={it} />
										))}
										{query.isLoading &&
											Array.from({length: 8}).map((_, i) => (
												<tr key={`sk-${i}`} className="border-b border-border/50">
													<td colSpan={11} className="space-y-1.5 px-2 py-2">
														<Skeleton w="w-1/3" h="h-3" />
													</td>
												</tr>
											))}
										{!query.isLoading && rows.length === 0 && (
											<tr>
												<td colSpan={11} className="px-5 py-12 text-center text-sm text-muted">
													<HandCoins className="mx-auto h-6 w-6 opacity-40" />
													<p className="mt-2">
														{items.length === 0
															? '该窗口内没有除净事件。'
															: '当前过滤条件下没有匹配的行。'}
													</p>
												</td>
											</tr>
										)}
									</tbody>
								</table>
							</section>

							<p className="text-[10px] leading-relaxed text-muted">
								股息率 = 每股派息(港币) ÷ 不复权收盘价 × 100, 是<strong className="font-medium text-secondary">单次派息</strong>口径而非 TTM;
								分母取最近一个有行情的交易日收盘(表内「收盘」列即该价, 停牌股会回落到更早的分区)。前复权价会把有分红史的
								标的股息率系统性算高, 故刻意不用。「—」表示算不出来, 与 0.00%(确认纯送转、无现金)是两种不同状态。
							</p>
						</>
					)}
				</div>
			</div>
		</div>
	)
}

function Row({it}: {it: HkDividendEvent}) {
	return (
		<tr className="border-b border-border/50 transition-colors hover:bg-elevated/40">
			<td className="whitespace-nowrap px-2 py-1.5 font-mono text-foreground">{it.ex_date}</td>
			<td className="whitespace-nowrap px-2 py-1.5 font-mono text-secondary">{it.symbol}</td>
			<td className="max-w-[10rem] truncate px-2 py-1.5 text-foreground" title={it.name ?? undefined}>
				{it.name ?? NA}
			</td>
			<td className="max-w-[8rem] truncate px-2 py-1.5 text-muted" title={it.industry ?? undefined}>
				{it.industry ?? NA}
			</td>
			<td className="max-w-[18rem] px-2 py-1.5 text-secondary">
				<span className={it.parsed ? '' : 'text-warning'} title={it.plan ?? undefined}>
					{it.plan ?? NA}
				</span>
				{!it.parsed && it.plan && (
					<span className="ml-1 rounded bg-warning/10 px-1 py-px text-[9px] text-warning" title="源数据未给港币折算值, 金额不可用">
						未解析
					</span>
				)}
			</td>
			<td className="whitespace-nowrap px-2 py-1.5">
				<ReportTypeBadge type={it.report_type} />
			</td>
			<td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-foreground">
				{it.dividend_hkd == null ? <span className="text-muted">{NA}</span> : it.dividend_hkd.toFixed(3)}
			</td>
			<td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-secondary">
				{it.bonus_ratio != null && it.bonus_ratio > 0 ? `${it.bonus_ratio.toFixed(2)}` : NA}
			</td>
			<td
				className="whitespace-nowrap px-2 py-1.5 text-right font-mono"
				title={
					it.dividend_yield_pct == null
						? it.raw_close == null
							? '取不到收盘价, 无法计算'
							: '每股派息未知, 无法计算'
						: `每股派息 ${it.dividend_hkd?.toFixed(3)} ÷ 收盘 ${it.raw_close} × 100`
				}
			>
				{it.dividend_yield_pct == null ? (
					<span className="text-muted">{NA}</span>
				) : (
					<span className={it.dividend_yield_pct >= 5 ? 'font-medium text-emerald-400' : 'text-foreground'}>
						{/* 🔴 后端给的已是百分比数值, 这里只拼 % —— 再乘 100 就错了 100 倍 */}
						{it.dividend_yield_pct.toFixed(2)}%
					</span>
				)}
			</td>
			<td className="whitespace-nowrap px-2 py-1.5 font-mono text-muted">{it.pay_date ?? NA}</td>
			<td className="whitespace-nowrap px-2 py-1.5 font-mono text-muted" title={it.price_date ? `价格日期 ${it.price_date}` : undefined}>
				{it.raw_close == null ? NA : it.raw_close.toFixed(2)}
			</td>
		</tr>
	)
}

function ReportTypeBadge({type}: {type: string | null}) {
	if (!type) return <span className="text-muted">{NA}</span>
	// 特别分配是异常高股息率的主要来源(实测榜首 40% 全是仙股特别派息), 单独标色
	const special = type.includes('特别')
	return (
		<span
			className={`rounded px-1.5 py-0.5 text-[10px] ${
				special ? 'bg-warning/10 text-warning' : 'bg-muted/10 text-secondary'
			}`}
		>
			{type}
		</span>
	)
}

function Stat({label, value, mono}: {label: string; value: string; mono?: boolean}) {
	return (
		<div className="inline-flex h-7 items-center gap-1.5 rounded-btn border border-border bg-surface/80 px-2.5 text-[11px]">
			<span className="text-muted">{label}</span>
			<span className={`${mono ? 'font-mono' : ''} font-medium text-foreground`}>{value}</span>
		</div>
	)
}

/** 数据质量计数。🔴 count 为 0 时不渲染 —— 但 >0 时必须可见, 不能藏进 tooltip。 */
function QualityFlag({count, label, hint}: {count: number; label: string; hint: string}) {
	if (!count) return null
	return (
		<span
			title={hint}
			className="inline-flex h-7 cursor-help items-center gap-1.5 rounded-btn border border-warning/30 bg-warning/5 px-2.5 text-[11px] text-warning"
		>
			<AlertTriangle className="h-3 w-3" />
			{label}
			<span className="rounded bg-warning/15 px-1 font-mono text-[10px]">{count}</span>
		</span>
	)
}

function Th({
	children, onClick, active, dir, right,
}: {
	children: ReactNode
	onClick: () => void
	active: boolean
	dir: 1 | -1
	right?: boolean
}) {
	return (
		<th className={`px-2 py-2 font-normal ${right ? 'text-right' : 'text-left'}`}>
			<button
				type="button"
				onClick={onClick}
				className={`inline-flex items-center gap-0.5 transition-colors hover:text-foreground ${
					active ? 'font-medium text-accent' : ''
				}`}
			>
				{children}
				<span className="text-[9px] opacity-70">{active ? (dir === 1 ? '▲' : '▼') : '↕'}</span>
			</button>
		</th>
	)
}
