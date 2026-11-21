import {useEffect, useMemo, useRef, useState} from 'react'
import {useQuery} from '@tanstack/react-query'
import * as echarts from 'echarts'
import {AlertTriangle, CalendarRange, RefreshCw, TrendingUp} from 'lucide-react'
import {api, ApiError, type HkConnectFlowRow} from '@/lib/api'
import {QK} from '@/lib/queryKeys'
import {PageHeader} from '@/components/PageHeader'
import {Skeleton} from '@/components/data/Skeleton'
import {useChartTheme} from '@/lib/theme'

/**
 * 港股通(**南向**)资金流。一行 = 一个交易日的净买入 / 成交额 / 沪深分通道 + 恒指对照。
 *
 * 🔴 三条口径约束, 改这个文件前先读 `HkConnectFlowRow` 的注释:
 * 1. 所有金额单位都是**元**(人民币), 后端已在数据源边界完成「百万元 → 元」换算。
 *    这里只做**展示级**的亿/万亿缩写, **绝不再乘除任何倍数**(§3.1)。
 * 2. `null` 与 `0` 是两种状态: `net_buy_5d` 为 null 表示「窗口不足 5 个交易日或
 *    其中某日缺数」, 渲染「—」; 渲染成 0 或渲染成部分和都是把缺陷伪装成数据(§4)。
 * 3. 上游失败(502)必须显示成**故障**, 不能落到「暂无数据」的空态里 ——
 *    「拉不到」和「这段时间没有资金流」是两件事。
 *
 * 🔴 页面上**没有**持股市值: 上游 `HOLD_MARKET_CAP` 口径无法定性(两种解释相差
 * 整整 2 倍)且无独立数据源可验证, 后端已在数据源层排除。不要从别处补一个进来。
 *
 * 数据**不落库**: 每次 query miss 都会打一次东财报表, 故 staleTime 给到 10 分钟,
 * 并且不提供自动轮询。
 */

/** 数据不落库 ⇒ 每次 miss 都打上游。10 分钟内复用缓存, 不自动刷新。 */
const STALE_MS = 600_000

/** 与后端 `HK_CONNECT_FLOW_MAX_WINDOW_DAYS` 对齐; 超了后端直接 400。 */
const MAX_WINDOW_DAYS = 1825

const PRESETS = [
	{label: '近 30 天', days: 30},
	{label: '近 90 天', days: 90},
	{label: '近 180 天', days: 180},
	{label: '近 1 年', days: 365},
	{label: '近 3 年', days: 1095},
] as const

/** A 股语义色: 红涨绿跌。资金净流入沿用同一套(流入=红, 流出=绿)。 */
const BULL = '#F04438'
const BEAR = '#12B76A'

/** 空值占位。🔴 不能换成 `0`: 那会把「算不出来」说成「没有」。 */
const NA = '—'

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

/**
 * 金额展示缩写(元 → 万/亿/万亿), **带符号**。
 *
 * 🔴 这是纯展示层缩写, 不是单位换算: 原始值始终是元, 缩写只改变可读性。
 * `lib/format.ts` 的 `fmtBigNum` 不能直接复用 —— 它按 `v >= 1e8` 判断, 负数会一路
 * 落到 `toFixed(0)`, 于是「净流出 118 亿」会显示成「-11811500000」。
 */
export function fmtYuan(v: number | null | undefined, digits = 2): string {
	if (v == null || Number.isNaN(v)) return NA
	const sign = v < 0 ? '-' : ''
	const a = Math.abs(v)
	if (a >= 1e12) return `${sign}${(a / 1e12).toFixed(digits)}万亿`
	if (a >= 1e8) return `${sign}${(a / 1e8).toFixed(digits)}亿`
	if (a >= 1e4) return `${sign}${(a / 1e4).toFixed(0)}万`
	return `${sign}${a.toFixed(0)}`
}

/** 净买入专用: 正值显式带 `+`, 让「流入/流出」在扫视时就能分辨。 */
export function fmtNetYuan(v: number | null | undefined): string {
	if (v == null || Number.isNaN(v)) return NA
	return v > 0 ? `+${fmtYuan(v)}` : fmtYuan(v)
}

function netColor(v: number | null | undefined): string {
	if (v == null || Number.isNaN(v) || v === 0) return 'text-muted'
	return v > 0 ? 'text-bull' : 'text-bear'
}

export function HkConnectFlow() {
	const today = useMemo(() => toIso(new Date()), [])
	const [start, setStart] = useState(() => shiftIso(today, -90))
	const [end, setEnd] = useState(() => today)

	const days = windowDays(start, end)
	const invalid = days < 0 || days > MAX_WINDOW_DAYS
	// 非法窗口不打请求: 后端会 400, 但让用户先看到原因比吃一个报错更好
	const query = useQuery({
		queryKey: QK.hkConnectFlow(start, end),
		queryFn: () => api.hkConnectFlow({start, end}),
		staleTime: STALE_MS,
		enabled: !invalid,
		retry: false,
	})

	const d = query.data
	// 后端已升序返回, 图表直接用; 明细表倒序展示(最新在上)
	const rowsDesc = useMemo(() => [...(d?.items ?? [])].reverse(), [d])

	const applyPreset = (p: (typeof PRESETS)[number]) => {
		setStart(shiftIso(today, -p.days))
		setEnd(today)
	}

	const upstreamDown = query.error instanceof ApiError && query.error.status === 502

	return (
		<div className="flex h-full flex-col">
			<PageHeader
				title="港股通资金流(南向)"
				subtitle="内地资金经港股通(沪/深)买卖港股的每日净买入 · 实时取自东财报表(不落库) · 金额单位为人民币元"
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
								const active = start === shiftIso(today, -p.days) && end === today
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
								aria-label="起始交易日"
							/>
							<span className="text-[11px] text-muted">至</span>
							<input
								type="date"
								value={end}
								min={start}
								onChange={e => e.target.value && setEnd(e.target.value)}
								className="h-7 rounded-btn border border-border bg-base px-2 font-mono text-[11px] text-foreground"
								aria-label="结束交易日"
							/>
							<span className={`text-[11px] ${invalid ? 'text-danger' : 'text-muted'}`}>
								共 {days} 天{invalid ? `(上限 ${MAX_WINDOW_DAYS} 天, 且起始不得晚于结束)` : ''}
							</span>
							{d && (
								<span className="ml-auto text-[11px] text-muted">
									{d.trading_days} 个交易日 · {d.currency} · {d.source}
								</span>
							)}
						</div>
					</section>

					{invalid ? (
						<div className="rounded-card border border-dashed border-danger/40 px-5 py-10 text-center text-sm text-danger">
							窗口不合法, 请调整日期后重试。
						</div>
					) : upstreamDown ? (
						/* 🔴 独立于空态: 上游挂了必须说「挂了」, 不能说「没有资金流」 */
						<div className="rounded-card border border-danger/40 bg-danger/5 px-5 py-10 text-center">
							<AlertTriangle className="mx-auto h-6 w-6 text-danger" />
							<p className="mt-2 text-sm font-medium text-danger">港股通资金流数据源不可用</p>
							<p className="mt-1 text-xs text-secondary">{(query.error as Error).message}</p>
							<p className="mt-2 text-[11px] text-muted">
								这不是「该窗口内没有资金流」, 而是上游报表取数失败。稍后重试, 或到 数据 页检查东财凭据与限流状态。
							</p>
						</div>
					) : query.error ? (
						<div className="rounded-card border border-danger/40 bg-danger/5 px-5 py-10 text-center text-sm text-danger">
							加载失败 · {(query.error as Error).message}
						</div>
					) : (
						<>
							{/* 概览卡片 */}
							<section className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-5">
								<Stat
									label={`最新净买入${d?.latest_trade_date ? ` · ${d.latest_trade_date}` : ''}`}
									value={fmtNetYuan(d?.latest_net_buy)}
									valueClass={netColor(d?.latest_net_buy)}
									hint="最近一个交易日的南向净买入(买入 − 卖出)。"
								/>
								<Stat
									label="近 5 日合计"
									value={fmtNetYuan(d?.net_buy_5d)}
									valueClass={netColor(d?.net_buy_5d)}
									hint={
										d?.net_buy_5d == null
											? '「—」表示窗口内不足 5 个交易日, 或其中某日上游缺数。后端刻意不给部分和: 3 天的合计冒充「近 5 日」在卡片上看不出来。'
											: '窗口内最近 5 个交易日的净买入合计。'
									}
								/>
								<Stat
									label="近 20 日合计"
									value={fmtNetYuan(d?.net_buy_20d)}
									valueClass={netColor(d?.net_buy_20d)}
									hint={
										d?.net_buy_20d == null
											? '「—」表示窗口内不足 20 个交易日, 或其中某日上游缺数。'
											: '窗口内最近 20 个交易日的净买入合计。'
									}
								/>
								<Stat
									label="历史累计净流入"
									value={fmtYuan(d?.accum_net_inflow)}
									hint="自 2014-11-17 港股通开通起的累计净流入, 由上游逐日维护(取最新一行即得, 不是本窗口求和)。"
								/>
								<Stat
									label="恒生指数"
									value={d?.index_close == null ? NA : d.index_close.toFixed(2)}
									valueClass={netColor(d?.index_change_pct)}
									suffix={
										d?.index_change_pct == null
											? undefined
											: `${d.index_change_pct > 0 ? '+' : ''}${d.index_change_pct.toFixed(2)}%`
									}
									hint="最新交易日的恒指收盘与当日涨跌幅, 用于与资金流对照。"
								/>
							</section>

							{/* 净买入柱 + 恒指线 */}
							<section className="rounded-card border border-border bg-surface p-3">
								<div className="mb-2 flex items-center gap-2 text-[11px] text-muted">
									<TrendingUp className="h-3.5 w-3.5" />
									每日净买入(柱, 左轴) 与 恒生指数(线, 右轴)
									<span className="ml-auto">红=净流入 · 绿=净流出</span>
								</div>
								<FlowChart rows={d?.items ?? []} />
							</section>

							{/* 逐日明细 */}
							<section className="overflow-hidden rounded-card border border-border bg-surface">
								<table className="w-full text-xs">
									<thead className="border-b border-border bg-base/60 text-[11px] text-muted">
										<tr>
											<th className="px-2 py-2 text-left font-normal">交易日</th>
											<th className="px-2 py-2 text-right font-normal">净买入</th>
											<th className="px-2 py-2 text-right font-normal">买入</th>
											<th className="px-2 py-2 text-right font-normal">卖出</th>
											<th className="px-2 py-2 text-right font-normal">成交额</th>
											<th className="px-2 py-2 text-right font-normal">港股通(沪)</th>
											<th className="px-2 py-2 text-right font-normal">港股通(深)</th>
											<th className="px-2 py-2 text-right font-normal">累计净流入</th>
											<th className="px-2 py-2 text-right font-normal">恒指</th>
											<th className="px-2 py-2 text-right font-normal">涨跌</th>
										</tr>
									</thead>
									<tbody>
										{rowsDesc.map(it => (
											<Row key={it.trade_date} it={it} />
										))}
										{query.isLoading &&
											Array.from({length: 10}).map((_, i) => (
												<tr key={`sk-${i}`} className="border-b border-border/50">
													<td colSpan={10} className="space-y-1.5 px-2 py-2">
														<Skeleton w="w-1/3" h="h-3" />
													</td>
												</tr>
											))}
										{!query.isLoading && rowsDesc.length === 0 && (
											<tr>
												<td colSpan={10} className="px-5 py-12 text-center text-sm text-muted">
													<TrendingUp className="mx-auto h-6 w-6 opacity-40" />
													<p className="mt-2">该窗口内没有交易日(整段假期或尚未开盘)。</p>
												</td>
											</tr>
										)}
									</tbody>
								</table>
							</section>

							<p className="text-[10px] leading-relaxed text-muted">
								净买入 = 买入 − 卖出, 成交额 = 买入 + 卖出, 港股通(沪) + 港股通(深) = 合计, 三条恒等式由上游保证。
								金额单位为<strong className="font-medium text-secondary">人民币元</strong>(后端已在数据源边界把上游的「百万元」
								换算完成, 页面只做亿/万亿的展示缩写)。币种 <code className="font-mono">{d?.currency ?? 'CNY'}</code> 是
								<strong className="font-medium text-secondary">推断</strong>而非上游声明(该报表无币种列), 依据记录在后端插件里。
								「—」表示上游该日缺数, 与 0 是两种不同状态。2016-12-05(深港通开通)之前港股通(深)各列为「—」。
								本页<strong className="font-medium text-secondary">不提供持股市值</strong>: 上游该列口径无法定性
								(两种解释相差整整 2 倍)且无独立数据源可验证, 已在数据源层排除。
							</p>
						</>
					)}
				</div>
			</div>
		</div>
	)
}

function Row({it}: {it: HkConnectFlowRow}) {
	return (
		<tr className="border-b border-border/50 transition-colors hover:bg-elevated/40">
			<td className="whitespace-nowrap px-2 py-1.5 font-mono text-foreground">{it.trade_date}</td>
			<td className={`whitespace-nowrap px-2 py-1.5 text-right font-mono font-medium ${netColor(it.net_buy)}`}>
				{fmtNetYuan(it.net_buy)}
			</td>
			<td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-secondary">{fmtYuan(it.buy)}</td>
			<td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-secondary">{fmtYuan(it.sell)}</td>
			<td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-muted">{fmtYuan(it.turnover)}</td>
			<td className={`whitespace-nowrap px-2 py-1.5 text-right font-mono ${netColor(it.ggt_sh_net_buy)}`}>
				{fmtNetYuan(it.ggt_sh_net_buy)}
			</td>
			<td
				className={`whitespace-nowrap px-2 py-1.5 text-right font-mono ${netColor(it.ggt_sz_net_buy)}`}
				title={it.ggt_sz_net_buy == null ? '深港通 2016-12-05 才开通, 或上游该日缺数' : undefined}
			>
				{fmtNetYuan(it.ggt_sz_net_buy)}
			</td>
			<td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-muted">{fmtYuan(it.accum_net_inflow)}</td>
			<td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-secondary">
				{it.index_close == null ? NA : it.index_close.toFixed(2)}
			</td>
			<td className={`whitespace-nowrap px-2 py-1.5 text-right font-mono ${netColor(it.index_change_pct)}`}>
				{/* 🔴 后端给的已是百分比数值, 这里只拼 % —— 再乘 100 就错了 100 倍 */}
				{it.index_change_pct == null
					? NA
					: `${it.index_change_pct > 0 ? '+' : ''}${it.index_change_pct.toFixed(2)}%`}
			</td>
		</tr>
	)
}

/** 净买入柱(左轴) + 恒指线(右轴)。跟随亮暗主题重设轴配色。 */
function FlowChart({rows}: {rows: HkConnectFlowRow[]}) {
	const elRef = useRef<HTMLDivElement>(null)
	const chartRef = useRef<echarts.ECharts | null>(null)
	const theme = useChartTheme()

	useEffect(() => {
		if (!elRef.current) return
		chartRef.current = echarts.init(elRef.current, undefined, {renderer: 'canvas'})
		const onResize = () => chartRef.current?.resize()
		window.addEventListener('resize', onResize)
		return () => {
			window.removeEventListener('resize', onResize)
			chartRef.current?.dispose()
			chartRef.current = null
		}
	}, [])

	useEffect(() => {
		const chart = chartRef.current
		if (!chart) return
		const hasIndex = rows.some(r => r.index_close != null)
		// 长窗口默认只放大最近 120 个交易日, 否则柱子挤成一条线
		const zoomStart = rows.length > 120 ? 100 - (120 / rows.length) * 100 : 0
		chart.setOption(
			{
				grid: {left: 64, right: hasIndex ? 64 : 16, top: 24, bottom: 44},
				tooltip: {
					trigger: 'axis',
					axisPointer: {type: 'shadow'},
					backgroundColor: theme.tooltipBg,
					borderColor: theme.tooltipBorder,
					textStyle: {color: theme.tooltipText, fontSize: 11},
					// 🔴 只在 tooltip 里做展示缩写, 原始值始终是元
					valueFormatter: (v: unknown) => (typeof v === 'number' ? fmtYuan(v) : NA),
				},
				dataZoom: [
					{type: 'inside', start: zoomStart, end: 100},
					{
						type: 'slider',
						height: 16,
						bottom: 8,
						start: zoomStart,
						end: 100,
						fillerColor: theme.zoomFill,
						borderColor: theme.border,
						textStyle: {color: theme.text, fontSize: 9},
					},
				],
				xAxis: {
					type: 'category',
					data: rows.map(r => r.trade_date),
					axisLine: {lineStyle: {color: theme.border}},
					axisLabel: {color: theme.text, fontSize: 10},
				},
				yAxis: [
					{
						type: 'value',
						name: '净买入',
						nameTextStyle: {color: theme.text, fontSize: 10},
						axisLabel: {color: theme.text, fontSize: 10, formatter: (v: number) => fmtYuan(v, 0)},
						splitLine: {lineStyle: {color: theme.grid}},
					},
					{
						type: 'value',
						name: '恒指',
						show: hasIndex,
						scale: true,
						nameTextStyle: {color: theme.text, fontSize: 10},
						axisLabel: {color: theme.text, fontSize: 10},
						splitLine: {show: false},
					},
				],
				series: [
					{
						name: '净买入',
						type: 'bar',
						data: rows.map(r => ({
							value: r.net_buy,
							// 流入红 / 流出绿, 与 A 股语义色一致
							itemStyle: {color: (r.net_buy ?? 0) >= 0 ? BULL : BEAR},
						})),
						barMaxWidth: 18,
					},
					{
						name: '恒生指数',
						type: 'line',
						yAxisIndex: 1,
						data: rows.map(r => r.index_close),
						showSymbol: false,
						connectNulls: false,
						lineStyle: {color: theme.text, width: 1.5, type: 'dashed'},
					},
				],
				legend: {
					top: 0,
					right: 0,
					textStyle: {color: theme.text, fontSize: 10},
					itemWidth: 14,
					data: ['净买入', '恒生指数'],
				},
			},
			{notMerge: true},
		)
	}, [rows, theme])

	return <div ref={elRef} className="h-64 w-full" role="img" aria-label="南向每日净买入与恒生指数走势" />
}

function Stat({
	label, value, suffix, valueClass, hint,
}: {
	label: string
	value: string
	suffix?: string
	valueClass?: string
	hint?: string
}) {
	return (
		<div title={hint} className="rounded-card border border-border bg-surface px-3 py-2">
			<div className="truncate text-[10px] text-muted">{label}</div>
			<div className="mt-0.5 flex items-baseline gap-1">
				<span className={`font-mono text-base font-medium ${valueClass ?? 'text-foreground'}`}>{value}</span>
				{suffix && <span className={`font-mono text-[11px] ${valueClass ?? 'text-muted'}`}>{suffix}</span>}
			</div>
		</div>
	)
}
