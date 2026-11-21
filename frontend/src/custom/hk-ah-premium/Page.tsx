import {useMemo, useState, type ReactNode} from 'react'
import {useQuery} from '@tanstack/react-query'
import {AlertTriangle, RefreshCw, Scale, Search} from 'lucide-react'
import {api, ApiError, type HkAhPremiumRow} from '@/lib/api'
import {QK} from '@/lib/queryKeys'
import {PageHeader} from '@/components/PageHeader'
import {Skeleton} from '@/components/data/Skeleton'

/**
 * AH 溢价 — 同一家公司的 A 股与 H 股价差。
 *
 * 🔴🔴 **汇率必须显式可见**。溢价率 = A收盘 ÷ (H收盘 × 港币汇率) − 1, 是本仓库
 * **唯一**被允许做汇率换算的指标, 而这个豁免是靠四个条件换来的: 明确来源 / 带日期 /
 * 响应与界面显式可见 / 取不到就 502。汇率错 1% 会让**每一只**标的的溢价线性偏移 1%,
 * 而且从数字上完全看不出来 —— 把汇率藏进 tooltip 等于拆掉第四根支柱。所以顶部横幅
 * 常驻显示 `fx_rate` / `fx_as_of` / `fx_source`, 不做折叠、不做「点击展开」。
 *
 * 🔴 四个数据质量计数(缺价 / 价格过期 / 日期错位 / 歧义丢弃)同样必须可见。
 * 它们不是错误提示, 而是「这张表有多可信」的一部分: 212 行里有 1 行缺价, 和
 * 212 行全齐, 是两种不同的结论。
 *
 * 🔴 `premium_pct` 已是百分比数值, 只拼 `%`, **绝不再乘 100**(§3.1)。
 * 🔴 `null` 渲染「—」, 不可渲染 0 —— 「A 股与 H 股同价」和「算不出来」是两件事。
 * 🔴 收盘价是**不复权原始价**。前复权价会把有分红史的标的溢价系统性算歪。
 *
 * 端点无参数(后端一次算全市场 200+ 对), 分页/排序/筛选全在前端做。每次 miss 会打
 * 两个上游 + 扫本地日K分区, 故 staleTime 给足, 不提供自动轮询。
 */

/** 一次请求成本 = 东财配对表 + 新浪汇率 + 扫最近 10 个日K分区。10 分钟内复用。 */
const STALE_MS = 600_000

/** 空值占位。🔴 不能换成 `0`: 那会把「算不出来」说成「A/H 同价」。 */
const NA = '—'

type SortKey = 'a_symbol' | 'h_symbol' | 'a_close' | 'h_close' | 'premium_pct'

/** 溢价分布分桶(百分比)。用于顶部那条分布带, 让「整体贵还是整体便宜」一眼可见。 */
const BUCKETS = [
	{label: '< -50%', test: (v: number) => v < -50},
	{label: '-50 ~ -20%', test: (v: number) => v >= -50 && v < -20},
	{label: '-20 ~ 0%', test: (v: number) => v >= -20 && v < 0},
	{label: '0 ~ 20%', test: (v: number) => v >= 0 && v < 20},
	{label: '20 ~ 50%', test: (v: number) => v >= 20 && v < 50},
	{label: '50 ~ 100%', test: (v: number) => v >= 50 && v < 100},
	{label: '≥ 100%', test: (v: number) => v >= 100},
] as const

/** null 永远沉底, 与排序方向无关。🔴 后端 polars 的 `nulls_last` 只管升序语义, 前端翻转方向时会再犯一次同坑。 */
function cmpNullLast<T extends string | number>(a: T | null, b: T | null, dir: 1 | -1): number {
	if (a == null && b == null) return 0
	if (a == null) return 1
	if (b == null) return -1
	if (a < b) return -dir
	if (a > b) return dir
	return 0
}

/** 溢价率展示: 正值 = A 股贵(溢价), 负值 = H 股贵(折价)。 */
function fmtPremium(v: number | null): string {
	if (v == null || Number.isNaN(v)) return NA
	return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

function premiumColor(v: number | null): string {
	if (v == null || Number.isNaN(v)) return 'text-muted'
	// A 股更贵 → 红(A 股语义色里的「涨」); H 股更贵 → 绿
	return v > 0 ? 'text-bull' : v < 0 ? 'text-bear' : 'text-muted'
}

function median(values: number[]): number | null {
	if (values.length === 0) return null
	const s = [...values].sort((a, b) => a - b)
	const mid = Math.floor(s.length / 2)
	return s.length % 2 === 0 ? (s[mid - 1] + s[mid]) / 2 : s[mid]
}

export function HkAhPremium() {
	const [sortKey, setSortKey] = useState<SortKey>('premium_pct')
	const [sortDir, setSortDir] = useState<1 | -1>(-1)
	const [q, setQ] = useState('')
	const [side, setSide] = useState<'all' | 'premium' | 'discount'>('all')

	const query = useQuery({
		queryKey: QK.hkAhPremium,
		queryFn: () => api.hkAhPremium(),
		staleTime: STALE_MS,
		retry: false,
	})

	const items = query.data?.items ?? []

	const rows = useMemo(() => {
		const kw = q.trim().toLowerCase()
		const filtered = items.filter(it => {
			if (side === 'premium' && !(it.premium_pct != null && it.premium_pct > 0)) return false
			if (side === 'discount' && !(it.premium_pct != null && it.premium_pct < 0)) return false
			if (!kw) return true
			return (
				it.a_symbol.toLowerCase().includes(kw) ||
				it.h_symbol.toLowerCase().includes(kw) ||
				(it.a_name ?? '').toLowerCase().includes(kw) ||
				(it.h_name ?? '').toLowerCase().includes(kw)
			)
		})
		return filtered.sort((a, b) => {
			const c = cmpNullLast(a[sortKey], b[sortKey], sortDir)
			if (c !== 0) return c
			// 溢价率并列(含都为 null)时用 A 股代码兜底, 保证顺序稳定
			return a.a_symbol < b.a_symbol ? -1 : a.a_symbol > b.a_symbol ? 1 : 0
		})
	}, [items, q, side, sortKey, sortDir])

	const stats = useMemo(() => {
		const premiums = items.map(it => it.premium_pct).filter((v): v is number => v != null)
		return {
			counted: premiums.length,
			median: median(premiums),
			aDear: premiums.filter(v => v > 0).length,
			hDear: premiums.filter(v => v < 0).length,
			max: premiums.length ? Math.max(...premiums) : null,
			min: premiums.length ? Math.min(...premiums) : null,
		}
	}, [items])

	const buckets = useMemo(
		() => BUCKETS.map(b => ({...b, n: items.filter(it => it.premium_pct != null && b.test(it.premium_pct)).length})),
		[items],
	)
	const bucketMax = Math.max(1, ...buckets.map(b => b.n))

	const toggleSort = (key: SortKey) => {
		if (key === sortKey) {
			setSortDir(d => (d === 1 ? -1 : 1))
			return
		}
		setSortKey(key)
		// 价格与溢价默认从大到小, 代码默认字典序
		setSortDir(key === 'a_symbol' || key === 'h_symbol' ? 1 : -1)
	}

	const upstreamDown = query.error instanceof ApiError && query.error.status === 502
	const d = query.data

	return (
		<div className="flex h-full flex-col">
			<PageHeader
				title="AH 溢价"
				subtitle="同一家公司的 A 股与 H 股价差 · 正值=A 股更贵(溢价), 负值=H 股更贵(折价) · 收盘价为不复权原始价"
				right={
					<button
						type="button"
						onClick={() => void query.refetch()}
						disabled={query.isFetching}
						title="重新向上游取数(东财配对表 + 新浪汇率)"
						className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border bg-surface/80 px-3 text-xs text-secondary transition-colors hover:text-foreground disabled:opacity-50"
					>
						<RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? 'animate-spin' : ''}`} />
						刷新
					</button>
				}
			/>
			<div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
				<div className="mx-auto max-w-7xl space-y-4">
					{/* 🔴🔴 汇率横幅 — 常驻, 不折叠。这是本指标唯一被允许做汇率换算的前提条件之一。 */}
					<section className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded-card border border-accent/30 bg-accent/5 px-4 py-2.5">
						<span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-accent">
							<Scale className="h-3.5 w-3.5" />
							港币汇率
						</span>
						<span className="font-mono text-sm font-medium text-foreground">
							{d ? d.fx_rate.toFixed(6) : NA}
							<span className="ml-1 text-[10px] font-normal text-muted">CNY / HKD</span>
						</span>
						<span className="text-[11px] text-secondary">
							数据日期 <span className="font-mono">{d?.fx_as_of ?? NA}</span>
						</span>
						<span className="text-[11px] text-secondary">
							来源 <span className="font-mono">{d?.fx_source ?? NA}</span>
						</span>
						<span className="ml-auto text-[10px] leading-relaxed text-muted">
							溢价率 = A收盘 ÷ (H收盘 × 汇率) − 1。汇率错 1% 会让每一只标的的溢价同向偏移 1%, 故此处常驻显示。
						</span>
					</section>

					{upstreamDown ? (
						/* 🔴 独立于空态: 上游挂了必须说「挂了」, 不能说「没有 AH 股」 */
						<div className="rounded-card border border-danger/40 bg-danger/5 px-5 py-10 text-center">
							<AlertTriangle className="mx-auto h-6 w-6 text-danger" />
							<p className="mt-2 text-sm font-medium text-danger">AH 溢价数据源不可用</p>
							<p className="mt-1 text-xs text-secondary">{(query.error as Error).message}</p>
							<p className="mt-2 text-[11px] text-muted">
								配对表(东财)或汇率(新浪)取数失败。后端刻意<strong className="font-medium text-secondary">不</strong>
								用 1.0 当汇率兜底 —— 那会让每一只标的的溢价都错约 17%。
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
								<Stat label="配对" value={String(d?.total ?? 0)} />
								<Stat label="已算出" value={String(stats.counted)} />
								<Stat label="中位溢价" value={fmtPremium(stats.median)} valueClass={premiumColor(stats.median)} />
								<Stat label="A 股更贵" value={String(stats.aDear)} valueClass="text-bull" />
								<Stat label="H 股更贵" value={String(stats.hDear)} valueClass="text-bear" />
								<Stat label="最高" value={fmtPremium(stats.max)} valueClass="text-bull" />
								<Stat label="最低" value={fmtPremium(stats.min)} valueClass="text-bear" />
								<div className="ml-auto flex flex-wrap items-center gap-2">
									<QualityFlag
										count={d?.missing_price_count ?? 0}
										label="缺收盘价"
										hint="A 侧或 H 侧在最近若干个日K分区里取不到收盘(未同步 / 长期停牌 / 已退市)。这些行仍在下表里, 溢价率显示「—」。"
									/>
									<QualityFlag
										count={d?.stale_price_count ?? 0}
										label="价格过期"
										hint="取到的收盘价距今超过 5 个自然日, 已判定为不可比, 溢价率置「—」而不是拿旧价硬算。"
									/>
									<QualityFlag
										count={d?.price_lag_count ?? 0}
										label="日期错位"
										hint="A 侧与 H 侧的收盘价不是同一天(两地假期不同)。跨日比价没有意义, 溢价率置「—」。"
									/>
									<QualityFlag
										count={d?.ambiguous_a_symbols?.length ?? 0}
										label="歧义丢弃"
										hint="一个 A 股代码对应多个 H 股柜台(如同时有 500 股/手与 2000 股/手两种), 且无法用「上市更早」唯一确定。后端刻意不猜, 整组丢弃并在此列出代码。"
										detail={d?.ambiguous_a_symbols}
									/>
								</div>
							</section>

							{/* 溢价分布 */}
							{stats.counted > 0 && (
								<section className="rounded-card border border-border bg-surface p-3">
									<div className="mb-2 text-[11px] text-muted">溢价分布(按已算出的 {stats.counted} 对)</div>
									<div className="flex flex-wrap items-end gap-2">
										{buckets.map(b => (
											<div key={b.label} className="flex w-24 flex-col items-center gap-1">
												<span className="font-mono text-[11px] text-foreground">{b.n}</span>
												<div className="flex h-16 w-full items-end justify-center rounded bg-base/60">
													<div
														className={`w-full rounded-t ${b.test(0) ? 'bg-bull/60' : 'bg-bear/60'}`}
														style={{height: `${(b.n / bucketMax) * 100}%`}}
													/>
												</div>
												<span className="text-[9px] text-muted">{b.label}</span>
											</div>
										))}
									</div>
								</section>
							)}

							{/* 过滤条 */}
							<section className="flex flex-wrap items-center gap-2">
								<div className="relative">
									<Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
									<input
										value={q}
										onChange={e => setQ(e.target.value)}
										placeholder="A/H 代码或名称"
										className="h-8 w-56 rounded-btn border border-border bg-surface pl-7 pr-2 text-xs text-foreground placeholder:text-muted"
									/>
								</div>
								{([
									['all', '全部'],
									['premium', 'A 股溢价'],
									['discount', 'H 股更贵'],
								] as const).map(([key, label]) => (
									<button
										key={key}
										type="button"
										onClick={() => setSide(key)}
										className={`h-8 rounded-btn border px-2.5 text-[11px] transition-colors ${
											side === key
												? 'border-accent/40 bg-accent/10 font-medium text-accent'
												: 'border-border bg-surface text-secondary hover:text-foreground'
										}`}
									>
										{label}
									</button>
								))}
								<span className="text-[11px] text-muted">
									显示 {rows.length} / {items.length} 行
								</span>
								{d && <span className="ml-auto text-[11px] text-muted">候选 {d.candidate_count} · {d.source}</span>}
							</section>

							{/* 明细表 */}
							<section className="overflow-hidden rounded-card border border-border bg-surface">
								<table className="w-full text-xs">
									<thead className="border-b border-border bg-base/60 text-[11px] text-muted">
										<tr>
											<Th onClick={() => toggleSort('a_symbol')} active={sortKey === 'a_symbol'} dir={sortDir}>
												A 股代码
											</Th>
											<th className="px-2 py-2 text-left font-normal">A 股名称</th>
											<Th onClick={() => toggleSort('a_close')} active={sortKey === 'a_close'} dir={sortDir} right>
												A 收盘
											</Th>
											<th className="px-2 py-2 text-left font-normal">A 价格日</th>
											<Th onClick={() => toggleSort('h_symbol')} active={sortKey === 'h_symbol'} dir={sortDir}>
												H 股代码
											</Th>
											<th className="px-2 py-2 text-left font-normal">H 股名称</th>
											<Th onClick={() => toggleSort('h_close')} active={sortKey === 'h_close'} dir={sortDir} right>
												H 收盘
											</Th>
											<th className="px-2 py-2 text-left font-normal">H 价格日</th>
											<Th
												onClick={() => toggleSort('premium_pct')}
												active={sortKey === 'premium_pct'}
												dir={sortDir}
												right
											>
												溢价率
											</Th>
										</tr>
									</thead>
									<tbody>
										{rows.map(it => (
											<Row key={it.a_symbol} it={it} fxRate={d?.fx_rate ?? null} />
										))}
										{query.isLoading &&
											Array.from({length: 12}).map((_, i) => (
												<tr key={`sk-${i}`} className="border-b border-border/50">
													<td colSpan={9} className="space-y-1.5 px-2 py-2">
														<Skeleton w="w-1/3" h="h-3" />
													</td>
												</tr>
											))}
										{!query.isLoading && rows.length === 0 && (
											<tr>
												<td colSpan={9} className="px-5 py-12 text-center text-sm text-muted">
													<Scale className="mx-auto h-6 w-6 opacity-40" />
													<p className="mt-2">
														{items.length === 0
															? '上游配对表返回空(或本地日K尚未同步)。'
															: '当前过滤条件下没有匹配的行。'}
													</p>
												</td>
											</tr>
										)}
									</tbody>
								</table>
							</section>

							<p className="text-[10px] leading-relaxed text-muted">
								配对关系取自东财 F10 公司资料(全市场 {d?.candidate_count ?? 0} 个候选), 收盘价取自本地日K分区的
								<strong className="font-medium text-secondary">不复权原始价</strong> —— 前复权价会把有分红史的标的溢价系统性算歪。
								一个 A 股代码对应多个 H 股柜台时, 取<strong className="font-medium text-secondary">上市更早</strong>的那个;
								上市日缺失或并列则整组丢弃(不猜), 计入上方「歧义丢弃」。ST 与已退市标的刻意保留(实测溢价榜首常是 ST 股)。
								「—」表示算不出来(缺价 / 过期 / 日期错位), 与 0.00%(A/H 恰好同价)是两种不同状态。
							</p>
						</>
					)}
				</div>
			</div>
		</div>
	)
}

function Row({it, fxRate}: {it: HkAhPremiumRow; fxRate: number | null}) {
	const hInCny = it.h_close != null && fxRate != null ? it.h_close * fxRate : null
	const lagged = it.a_price_date != null && it.h_price_date != null && it.a_price_date !== it.h_price_date
	return (
		<tr className="border-b border-border/50 transition-colors hover:bg-elevated/40">
			<td className="whitespace-nowrap px-2 py-1.5 font-mono text-foreground">{it.a_symbol}</td>
			<td className="max-w-[9rem] truncate px-2 py-1.5 text-secondary" title={it.a_name ?? undefined}>
				{it.a_name ?? NA}
			</td>
			<td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-foreground">
				{it.a_close == null ? <span className="text-muted">{NA}</span> : it.a_close.toFixed(2)}
			</td>
			<td className="whitespace-nowrap px-2 py-1.5 font-mono text-muted">{it.a_price_date ?? NA}</td>
			<td className="whitespace-nowrap px-2 py-1.5 font-mono text-secondary">{it.h_symbol}</td>
			<td className="max-w-[9rem] truncate px-2 py-1.5 text-secondary" title={it.h_name ?? undefined}>
				{it.h_name ?? NA}
			</td>
			<td
				className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-foreground"
				title={hInCny == null ? undefined : `折人民币 ${hInCny.toFixed(3)} 元 (× 汇率 ${fxRate?.toFixed(6)})`}
			>
				{it.h_close == null ? <span className="text-muted">{NA}</span> : it.h_close.toFixed(3)}
				<span className="ml-1 text-[9px] text-muted">HKD</span>
			</td>
			<td className={`whitespace-nowrap px-2 py-1.5 font-mono ${lagged ? 'text-warning' : 'text-muted'}`}>
				{it.h_price_date ?? NA}
			</td>
			<td
				className={`whitespace-nowrap px-2 py-1.5 text-right font-mono font-medium ${premiumColor(it.premium_pct)}`}
				title={
					it.premium_pct == null
						? it.a_close == null || it.h_close == null
							? '缺收盘价, 无法计算'
							: lagged
								? `A/H 价格日期不一致 (${it.a_price_date} vs ${it.h_price_date}), 跨日比价无意义`
								: '价格过期, 无法计算'
						: `A ${it.a_close?.toFixed(2)} ÷ (H ${it.h_close?.toFixed(3)} × ${fxRate?.toFixed(6)}) − 1`
				}
			>
				{/* 🔴 后端给的已是百分比数值, 这里只拼 % —— 再乘 100 就错了 100 倍 */}
				{fmtPremium(it.premium_pct)}
			</td>
		</tr>
	)
}

function Stat({label, value, valueClass}: {label: string; value: string; valueClass?: string}) {
	return (
		<div className="inline-flex h-7 items-center gap-1.5 rounded-btn border border-border bg-surface/80 px-2.5 text-[11px]">
			<span className="text-muted">{label}</span>
			<span className={`font-mono font-medium ${valueClass ?? 'text-foreground'}`}>{value}</span>
		</div>
	)
}

/**
 * 数据质量计数。🔴 count 为 0 时不渲染 —— 但 >0 时必须可见, 不能藏进 tooltip。
 * `detail` 用于「歧义丢弃」这种需要点名到具体代码的情况。
 */
function QualityFlag({count, label, hint, detail}: {count: number; label: string; hint: string; detail?: string[]}) {
	if (!count) return null
	const title = detail && detail.length > 0 ? `${hint}\n\n涉及: ${detail.join(', ')}` : hint
	return (
		<span
			title={title}
			className="inline-flex h-7 cursor-help items-center gap-1.5 rounded-btn border border-warning/30 bg-warning/5 px-2.5 text-[11px] text-warning"
		>
			<AlertTriangle className="h-3 w-3" />
			{label}
			<span className="rounded bg-warning/15 px-1 font-mono text-[10px]">{count}</span>
			{detail && detail.length > 0 && (
				<span className="max-w-[16rem] truncate font-mono text-[10px] opacity-80">{detail.join(', ')}</span>
			)}
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
