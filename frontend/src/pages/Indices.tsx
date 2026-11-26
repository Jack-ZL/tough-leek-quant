import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Loader2, Lock, RefreshCw } from 'lucide-react'
import { api, type MinuteKlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCapabilities } from '@/lib/useSharedQueries'
import type { OHLC } from '@/lib/chart-types'
import { PageHeader } from '@/components/PageHeader'
import { LightweightDaily } from '@/components/charts/LightweightDaily'
import { LightweightIntraday } from '@/components/charts/LightweightIntraday'
import { QuoteDelayBadge } from '@/components/QuoteDelayBadge'
import { DEFAULT_MARKET, MARKETS, normalizeMarket, type Market } from '@/lib/market'
import { marketContent, type CoreIndex } from '@/lib/marketContent'
import { indexPageParams, indexSource, type IndexChartRow } from '@/lib/index-market-adapter'
import { setMarket, useMarket } from '@/lib/marketStore'

function defaultRange() {
	const now = new Date()
	const end = now.toISOString().slice(0, 10)
	const s = new Date(now)
	s.setMonth(s.getMonth() - 6)
	return { start: s.toISOString().slice(0, 10), end }
}

function toOHLC(rows: readonly IndexChartRow[]): OHLC[] {
	return rows
		.filter(r => r?.date != null && r.open != null && r.close != null)
		.map(r => ({
			date: typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date),
			open: Number(r.open),
			high: Number(r.high),
			low: Number(r.low),
			close: Number(r.close),
			volume: Number(r.volume ?? 0),
			ma5: r.ma5 != null ? Number(r.ma5) : null,
			ma10: r.ma10 != null ? Number(r.ma10) : null,
			ma20: r.ma20 != null ? Number(r.ma20) : null,
			ma60: r.ma60 != null ? Number(r.ma60) : null,
			macd_dif: r.macd_dif != null ? Number(r.macd_dif) : null,
			macd_dea: r.macd_dea != null ? Number(r.macd_dea) : null,
			macd_hist: r.macd_hist != null ? Number(r.macd_hist) : null,
			rsi_6: r.rsi_6 != null ? Number(r.rsi_6) : null,
			rsi_14: r.rsi_14 != null ? Number(r.rsi_14) : null,
			rsi_24: r.rsi_24 != null ? Number(r.rsi_24) : null,
			kdj_k: r.kdj_k != null ? Number(r.kdj_k) : null,
			kdj_d: r.kdj_d != null ? Number(r.kdj_d) : null,
			kdj_j: r.kdj_j != null ? Number(r.kdj_j) : null,
			boll_upper: r.boll_upper != null ? Number(r.boll_upper) : null,
			boll_lower: r.boll_lower != null ? Number(r.boll_lower) : null,
		}))
}

function fmtPct(v: number | null | undefined) {
	if (v == null || Number.isNaN(Number(v))) return '--'
	return `${Number(v).toFixed(2)}%`
}

function fmtNum(v: number | null | undefined, digits = 2) {
	if (v == null || Number.isNaN(Number(v))) return '--'
	return Number(v).toFixed(digits)
}

export function Indices() {
	const qc = useQueryClient()
	const [searchParams, setSearchParams] = useSearchParams()
	const symbolParam = searchParams.get('symbol') ?? ''
	const urlMarket = searchParams.get('market')
	const globalMarket = useMarket()
	// 市场优先取 URL(可分享、刷新不丢); URL 未声明时跟随侧栏全局切换器。
	// 未知值兜底 CN —— 这是**展示层**读取, 允许兜底 (lib/market.ts 分流铁律);
	// 真正拼请求参数时走的是 src.* 适配器, 不吃这个兜底。
	const market = normalizeMarket(urlMarket ?? globalMarket)
	const src = indexSource(market)
	const [selected, setSelected] = useState<string>(symbolParam)
	const [range, setRange] = useState(defaultRange)
	const [selectedDate, setSelectedDate] = useState<string | null>(null)
	const [linkedPrice, setLinkedPrice] = useState<number | null>(null)

	// 分时数据依赖分钟K批量数据 (kline.minute.batch); 港股根本没有指数分时端点。
	const caps = useCapabilities()
	const hasMinuteCap = !!caps.data?.capabilities?.['kline.minute.batch']
	const minuteAvailable = hasMinuteCap && src.supportsMinute

	// 指数标的固定核心四只 (产品契约, 不再提供全指数搜索/浏览)。
	// 清单住在 lib/marketContent.ts, 按当前市场取。
	const pinnedSymbols = useMemo(() => new Set(src.pinned.map(i => i.symbol)), [src])

	// 🔴 URL 里的 symbol 可能属于另一个市场(例如 `?symbol=HSI` 却没带 market)。
	// 过滤类请求不兜底(方案 §8 规则 6): 不属于当前市场就当没选, 回落第一只 ——
	// 绝不拿港股代码去查 A 股指数目录(会静默返回空, 比报错难发现)。
	const selectedSymbol = useMemo(() => {
		const wanted = selected || symbolParam
		return pinnedSymbols.has(wanted) ? wanted : (src.pinned[0]?.symbol ?? '')
	}, [selected, symbolParam, pinnedSymbols, src])

	useEffect(() => {
		if (symbolParam && symbolParam !== selected) setSelected(symbolParam)
	}, [selected, symbolParam])

	// URL ↔ 全局市场 store 双向对齐 (两个方向都必须幂等, 否则会互相触发成死循环):
	//  - URL 显式带 market → 以 URL 为准, 回写 store, 让侧栏指数条与本页同市场;
	//  - URL 没带但 store 已是港股 → 用 replace 把 market 补进 URL, 否则这条链接
	//    分享/刷新出去会静默退回 A 股(用户明明在侧栏切了港股)。
	useEffect(() => {
		if (urlMarket != null) {
			const fromUrl = normalizeMarket(urlMarket)
			if (fromUrl !== globalMarket) setMarket(fromUrl)
			return
		}
		if (globalMarket !== DEFAULT_MARKET) {
			setSearchParams(indexPageParams(globalMarket, symbolParam || undefined), { replace: true })
		}
	}, [urlMarket, globalMarket, symbolParam, setSearchParams])

	const selectIndex = (symbol: string) => {
		setSelected(symbol)
		setSearchParams(indexPageParams(market, symbol))
	}

	const selectMarket = (next: Market) => {
		if (next === market) return
		// 切市场必须清掉选中标的: 两个市场的代码空间不通用。
		setSelected('')
		setSelectedDate(null)
		setLinkedPrice(null)
		// 🔴 必须同步写全局 store: 切回 A 股时 indexPageParams 会把 market 从 URL 里
		// 省掉(保持旧链接形态), 若 store 还停在 HK, 上面的对齐 effect 会立刻把
		// market=HK 写回 URL —— 页内切换器就切不动了。
		setMarket(next)
		setSearchParams(indexPageParams(next))
	}

	const quotes = useQuery({
		queryKey: src.quotesKey,
		queryFn: () => src.fetchQuotes(),
		// 🔴 跨市场不得沿用旧数据(否则港股页会短暂显示上证指数的数字, 正是方案 §8
		// 引用的「标题改了、口径没改, 比不切换更误导」); 同市场内保留以避免闪烁。
		placeholderData: (prev, prevQuery) =>
			prevQuery?.queryKey[0] === src.quotesKey[0] ? prev : undefined,
	})

	const dailyKey = src.dailyKey(selectedSymbol, range.start, range.end)
	const daily = useQuery({
		queryKey: dailyKey,
		queryFn: () => src.fetchDaily(selectedSymbol, range),
		enabled: !!selectedSymbol,
		placeholderData: (prev, prevQuery) =>
			prevQuery?.queryKey[0] === dailyKey[0] ? prev : undefined,
	})

	const minute = useQuery({
		queryKey: QK.indexMinute(selectedSymbol, selectedDate ?? ''),
		queryFn: () => api.indexMinute(selectedSymbol, selectedDate ?? undefined),
		enabled: !!selectedSymbol && !!selectedDate && minuteAvailable,
	})

	const syncDaily = useMutation({
		mutationFn: () => src.syncDaily(),
		onSuccess: () => {
			for (const key of src.invalidateKeys) qc.invalidateQueries({ queryKey: key })
		},
	})

	const quoteBySymbol = useMemo(() => {
		const m = new Map<string, any>()
		for (const q of quotes.data?.rows ?? []) m.set(q.symbol, q)
		return m
	}, [quotes.data?.rows])
	const selectedQuote = selectedSymbol ? quoteBySymbol.get(selectedSymbol) : null
	const selectedQuoteValue = selectedQuote?.last_price ?? selectedQuote?.price ?? selectedQuote?.close
	const selectedQuotePct = selectedQuote?.change_pct ?? selectedQuote?.pct

	const chartRows = useMemo(() => toOHLC(daily.data?.rows ?? []), [daily.data?.rows])
	const selectedName = src.pinned.find(i => i.symbol === selectedSymbol)?.name
		?? daily.data?.name ?? null
	const minuteRows: MinuteKlineRow[] = minute.data?.symbol === selectedSymbol
		&& minute.data?.date === selectedDate && daily.data?.symbol === selectedSymbol
		? minute.data.rows : []
	const selectedIdx = selectedDate ? chartRows.findIndex(r => r.date === selectedDate) : -1
	const prevClose = selectedIdx > 0
		? chartRows[selectedIdx - 1].close
		: chartRows.length >= 2
			? chartRows[chartRows.length - 2].close
			: undefined

	useEffect(() => {
		setSelectedDate(null)
		setLinkedPrice(null)
	}, [selectedSymbol])

	useEffect(() => {
		if ((!selectedDate || !chartRows.some(r => r.date === selectedDate)) && chartRows.length > 0 && daily.data?.symbol === selectedSymbol) {
			setSelectedDate(chartRows[chartRows.length - 1].date)
		}
	}, [chartRows, daily.data?.symbol, selectedDate, selectedSymbol])
	const renderIndexItem = (item: CoreIndex) => {
		const q = quoteBySymbol.get(item.symbol)
		const pct = q?.change_pct ?? q?.pct
		const current = q?.last_price ?? q?.price ?? q?.close
		const active = item.symbol === selectedSymbol
		return (
			<button
				key={item.symbol}
				onClick={() => selectIndex(item.symbol)}
				className={`w-full rounded-btn px-2 py-2 text-left transition-colors ${active ? 'bg-accent/15 text-foreground' : 'hover:bg-elevated text-secondary'}`}
			>
				<div className="flex items-center justify-between gap-2">
					<span className="truncate text-xs font-medium">{item.name || item.symbol}</span>
					<span className={`text-[10px] font-mono ${Number(pct ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtPct(pct)}</span>
				</div>
				<div className="mt-0.5 flex items-center justify-between text-[10px] font-mono text-muted">
					<span>{item.symbol}</span>
					<span>{fmtNum(current)}</span>
				</div>
			</button>
		)
	}

	return (
		<div className="flex h-full min-h-0 min-w-0 flex-col bg-base">
			<PageHeader
				title="指数"
				subtitle={
					<span title={src.headerHint}>
						{src.headerTagline}
						<span className="text-muted/70"> · {src.headerHint}</span>
					</span>
				}
				className="shrink-0 max-md:pl-16"
				titleExtra={
					/* 市场切换。差异全在 lib/index-market-adapter.ts + marketContent.ts,
					   本页不出现任何市场字面量分支(方案 §8 规则 2)。 */
					<div role="tablist" aria-label="市场" className="inline-flex shrink-0 rounded-btn border border-border p-0.5">
						{MARKETS.map(m => (
							<button
								key={m}
								type="button"
								role="tab"
								aria-selected={m === market}
								onClick={() => selectMarket(m)}
								className={`rounded px-2 py-0.5 text-xs transition-colors ${m === market ? 'bg-accent text-white' : 'text-secondary hover:bg-elevated'}`}
							>
								{marketContent(m).label}
							</button>
						))}
					</div>
				}
				right={
					<button
						type="button"
						onClick={() => syncDaily.mutate()}
						disabled={syncDaily.isPending}
						aria-busy={syncDaily.isPending}
						className="inline-flex min-h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-btn bg-accent px-2.5 text-xs font-medium text-white transition-colors hover:bg-accent/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 [@media(pointer:coarse)]:min-h-9"
					>
						{syncDaily.isPending ? <Loader2 aria-hidden="true" className="size-3.5 animate-spin" /> :
							<RefreshCw aria-hidden="true" className="size-3.5" />}
						{src.syncLabel}
					</button>
				}
			/>

			<div className="grid min-h-0 flex-1 grid-cols-1 content-start gap-3 overflow-auto p-3 md:px-5 lg:grid-cols-[15rem_minmax(0,1fr)]">
				<aside className="rounded-card border border-border bg-surface p-3">
					<div className="mb-2 px-1 text-[11px] uppercase tracking-wider text-muted">核心指数</div>
					<div className="grid grid-cols-2 gap-1 lg:block lg:space-y-1">
						{src.pinned.map(renderIndexItem)}
					</div>
				</aside>

				<section aria-label="指数行情" className="min-w-0 rounded-card border border-border bg-surface p-3">
					<div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
						<div className="min-w-0">
							<div className="flex flex-wrap items-center gap-x-2 gap-y-1">
								<Activity className="h-4 w-4 text-accent" />
								<h2 className="truncate text-sm font-semibold text-foreground">
									{selectedName || selectedSymbol || '未选择指数'}
								</h2>
								{selectedSymbol &&
									<span className="font-mono text-xs text-muted">{selectedSymbol}</span>}
								{selectedSymbol &&
									<span className="font-mono text-xs text-foreground">{fmtNum(selectedQuoteValue)}</span>}
								{selectedSymbol &&
									<span className={`font-mono text-xs ${Number(selectedQuotePct ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{fmtPct(selectedQuotePct)}</span>}
							</div>
							<div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
								<span>{src.quoteSourceLabel(quotes.data)}</span>
								{daily.data?.source && <span>· 日K来源 {daily.data.source}</span>}
								{/* 🔴 分钟数只从接口 quote_delay_minutes 读; A 股为 null 故不渲染 */}
								<QuoteDelayBadge minutes={quotes.data?.quoteDelayMinutes} />
							</div>
						</div>
						<div className="flex min-w-0 items-center gap-2 text-xs">
							<input
								type="date"
								aria-label="日K开始日期"
								value={range.start}
								onChange={e => setRange(r => ({ ...r, start: e.target.value }))}
								className="min-w-0 rounded-btn border border-border bg-base px-2 py-1 text-secondary outline-none focus:border-accent"
							/>
							<span className="text-muted">至</span>
							<input
								type="date"
								aria-label="日K结束日期"
								value={range.end}
								onChange={e => setRange(r => ({ ...r, end: e.target.value }))}
								className="min-w-0 rounded-btn border border-border bg-base px-2 py-1 text-secondary outline-none focus:border-accent"
							/>
						</div>
					</div>

					{daily.isLoading && <div className="py-10 text-center text-sm text-muted">日K加载中…</div>}
					{daily.isError && <div className="py-4 text-sm text-danger">指数日K加载失败</div>}
					{!daily.isLoading && !daily.isError && chartRows.length === 0 && (
						<div className="rounded-card bg-elevated p-6 text-center text-sm text-muted">
							{src.dailyEmptyHint}
						</div>
					)}
					{chartRows.length > 0 && (
						<div className="flex flex-col gap-3 lg:flex-row lg:items-start">
							<div className="min-w-0 flex-1">
								<LightweightDaily
									data={chartRows}
									height={620}
									showMA={true}
									showInfoBar={true}
									symbol={selectedSymbol}
									linkedPrice={linkedPrice}
									onDateClick={(date) => {
										setSelectedDate(date)
										setLinkedPrice(null)
									}}
									visibleBars={48}
									activeIndicators={['vol', 'macd']}
								/>
							</div>
							<div className="min-w-0 lg:flex-1 lg:border-l lg:border-border lg:pl-3" style={{ height: 620 }}>
								{!minuteAvailable ? (
									<div className="flex h-full flex-col items-center justify-center gap-2 text-center">
										<Lock className="h-5 w-5 text-muted" />
										<div className="text-xs text-secondary">指数分时数据不可用</div>
										<div className="text-[10px] text-muted">{src.minuteUnavailableHint}</div>
									</div>
								) : (
									<>
										{minute.isLoading && <div className="py-2 text-xs text-muted">分时加载中…</div>}
										{!minute.isLoading && minuteRows.length === 0 && (
											<div className="flex h-full items-center justify-center text-xs text-muted">
												暂无分时数据
											</div>
										)}
										{minuteRows.length > 0 && (
											<LightweightIntraday
												key={`${selectedSymbol}:${selectedDate}`}
												symbol={selectedSymbol}
												assetType="index"
												data={minuteRows}
												height={620}
												prevClose={prevClose}
												date={selectedDate ?? undefined}
												showLimitLines={false}
												showAvgLine={false}
												onPriceHover={setLinkedPrice}
											/>
										)}
									</>
								)}
							</div>
						</div>
					)}
				</section>
			</div>
		</div>
	)
}
