import {useEffect, useMemo, useRef, useState} from 'react'
import {useQuery} from '@tanstack/react-query'
import * as echarts from 'echarts'
import {Activity, AlertTriangle, RefreshCw} from 'lucide-react'
import {api, type HkVolatilityPoint, type HkVolatilityResponse} from '@/lib/api'
import {QK} from '@/lib/queryKeys'
import {PageHeader} from '@/components/PageHeader'
import {Skeleton} from '@/components/data/Skeleton'
import {useChartTheme} from '@/lib/theme'

/**
 * 港股波动率情绪 (VHSI 恒生波动率指数)。
 *
 * 这是 A 股「情绪周期」在港股的**对应维度**, 不是同一指标的港股版: A 股那套建立在
 * 连板梯队上, 而港股无涨跌停 ⇒ 连板结构上不存在。
 *
 * 🔴🔴 改这个文件前先读 `HkVolatilityResponse` 的注释, 三条口径约束:
 * 1. **单位**: `level` 是年化隐含波动率的**百分点数** —— `24.09` 即 `24.09%`,
 *    **不是**小数, 这里**绝不能再 ×100**(会显示成 2409%)。`change_pct_1d` 同为
 *    百分数, 与项目里 `change_pct` 常见的小数口径**相反**。
 * 2. **档位是相对标尺**: `regime` 由 `percentile`(样本内分位排名)落档, 按构造
 *    样本里恒有约 10% 的日子是「极低波」、约 2% 是「恐慌」。它回答「现在比过去
 *    紧张还是宽松」, **不**回答「绝对波动率高不高」。故分位阈值表与样本三件套
 *    必须同屏可见, 否则用户无法查证今天的判定。
 * 3. **样本不足 fail closed**: `insufficient_history=true` 时 `regime`/`percentile`
 *    /`streak_days`/`corr_hsi` 全为 `null`, 必须渲染成「样本不足」并给出原因,
 *    **不得**落到任何默认档 —— 那是把「算不出来」伪装成「正常」。
 *
 * 🔴 后端对 VHSI **不编展示名**(`name` 恒为 null), 标题由本文件提供。
 * 🔴 `quote_delay_minutes` 从响应读, **不硬编码** 15。
 */

/** 后端对 VHSI 不注册展示名(它不在四个核心指数里), 故标题在前端适配器给出。 */
const TITLE = '港股波动率情绪 (VHSI)'

/** 纯本地读, 但一次响应带数百个绘图点 ⇒ 仍给 staleTime, 避免切 tab 就重取。 */
const STALE_MS = 300_000

/** 绘图序列长度预设(只裁剪 `series`, **不影响**档位标定样本)。 */
const SERIES_PRESETS = [
	{label: '近 90 天', days: 90},
	{label: '近 250 天', days: 250},
	{label: '近 500 天', days: 500},
	{label: '近 1000 天', days: 1000},
] as const

/** 空值占位。🔴 不能换成 `0`: 那会把「算不出来」说成「没有」。 */
const NA = '—'

/**
 * 档位配色 —— **风险递进**刻度, 刻意不用 `text-bull`/`text-bear`。
 *
 * 🔴 本项目的红绿是**涨跌**语义(红涨绿跌)。波动率档位若沿用, 「恐慌」会显示成
 * 红色而被读成「涨/好」, 「极低波」显示成绿色而被读成「跌/坏」—— 正好反了。
 * 故这里走 灰→蓝→中性→琥珀→红 的风险刻度, 与涨跌色在视觉上区分开。
 *
 * 🔴 未知档位键回落到 `fallback`(§7 展示层允许对未知值兜底): 后端加档时前端
 * 不该崩, 但也不能静默当成「常态」。
 */
const BAND_STYLE: Record<string, {text: string; chip: string; hex: string}> = {
	ultra_low: {text: 'text-muted', chip: 'border-border bg-base text-muted', hex: '#A1A1AA'},
	low: {text: 'text-accent', chip: 'border-accent/40 bg-accent/10 text-accent', hex: '#3B82F6'},
	normal: {text: 'text-foreground', chip: 'border-border bg-base text-foreground', hex: '#71717A'},
	elevated: {text: 'text-warning', chip: 'border-warning/40 bg-warning/10 text-warning', hex: '#F79009'},
	high: {text: 'text-danger', chip: 'border-danger/40 bg-danger/10 text-danger', hex: '#F04438'},
	panic: {text: 'text-danger', chip: 'border-danger/60 bg-danger/15 text-danger', hex: '#B42318'},
}
const FALLBACK_STYLE = {text: 'text-muted', chip: 'border-border bg-base text-muted', hex: '#A1A1AA'}

function bandStyle(key: string | null | undefined) {
	if (!key) return FALLBACK_STYLE
	return BAND_STYLE[key] ?? FALLBACK_STYLE
}

/** 点位展示: 原样输出, **绝不 ×100**(后端给的已是百分点数)。 */
function fmtLevel(v: number | null | undefined, digits = 2): string {
	return v == null || Number.isNaN(v) ? NA : v.toFixed(digits)
}

/** 涨跌幅: 后端给的已是百分数, 这里只拼 `%` 与符号。 */
function fmtPct(v: number | null | undefined, digits = 2): string {
	if (v == null || Number.isNaN(v)) return NA
	return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
}

/** 相关系数: null 渲染「—」, **不渲染 0**(0 会被读成「实测不相关」)。 */
function fmtCorr(v: number | null | undefined): string {
	return v == null || Number.isNaN(v) ? NA : v.toFixed(3)
}

export function HkVolatility() {
	const [seriesDays, setSeriesDays] = useState<number>(250)

	const query = useQuery({
		queryKey: QK.hkVolatility(seriesDays),
		queryFn: () => api.hkVolatility(seriesDays),
		staleTime: STALE_MS,
		retry: false,
	})

	const d = query.data
	const insufficient = d?.insufficient_history === true
	const style = bandStyle(d?.regime)

	return (
		<div className="flex h-full flex-col">
			<PageHeader
				title={TITLE}
				subtitle="恒生波动率指数 VHSI · 年化隐含波动率(百分点) · 档位按自身历史分位标定, 非 VIX 的 20/30 惯例"
				titleExtra={
					d?.quote_delay_minutes != null ? (
						<span className="shrink-0 rounded-btn border border-border bg-base px-1.5 py-0.5 text-[10px] text-muted">
							行情延迟 {d.quote_delay_minutes} 分钟
						</span>
					) : undefined
				}
				right={
					<button
						type="button"
						onClick={() => void query.refetch()}
						disabled={query.isFetching}
						title="重新读取本地数据"
						className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border bg-surface/80 px-3 text-xs text-secondary transition-colors hover:text-foreground disabled:opacity-50"
					>
						<RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? 'animate-spin' : ''}`} />
						刷新
					</button>
				}
			/>
			<div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
				<div className="mx-auto max-w-7xl space-y-4">
					{/* 绘图序列长度选择 */}
					<section className="rounded-card border border-border bg-surface p-3">
						<div className="flex flex-wrap items-center gap-2">
							<Activity className="h-4 w-4 shrink-0 text-muted" />
							<span className="text-[11px] text-muted">绘图区间</span>
							{SERIES_PRESETS.map(p => (
								<button
									key={p.days}
									type="button"
									onClick={() => setSeriesDays(p.days)}
									className={`h-7 rounded-btn border px-2.5 text-[11px] transition-colors ${
										seriesDays === p.days
											? 'border-accent/40 bg-accent/10 font-medium text-accent'
											: 'border-border bg-base text-secondary hover:text-foreground'
									}`}
								>
									{p.label}
								</button>
							))}
							{/* 🔴 必须写明: 这个选择**不**改变档位标定样本 */}
							<span className="text-[11px] text-muted">
								(只裁剪绘图序列, 档位始终按全样本标定)
							</span>
							{d && (
								<span className="ml-auto text-[11px] text-muted">
									标定样本 {d.sample_days} 个交易日
									{d.sample_start && d.sample_end ? ` · ${d.sample_start} ~ ${d.sample_end}` : ''}
								</span>
							)}
						</div>
					</section>

					{query.error ? (
						<div className="rounded-card border border-danger/40 bg-danger/5 px-5 py-10 text-center text-sm text-danger">
							加载失败 · {(query.error as Error).message}
						</div>
					) : query.isLoading ? (
						<div className="space-y-2">
							<Skeleton h="h-20" />
							<Skeleton h="h-64" />
						</div>
					) : !d ? (
						<div className="rounded-card border border-dashed border-border px-5 py-10 text-center text-sm text-muted">
							暂无数据
						</div>
					) : insufficient ? (
						/* 🔴 独立于空态与错误态: 这是「本地样本不够, 算不出档位」,
						   既不是「没有数据」也不是「取数失败」。必须给出可操作的原因。 */
						<div className="rounded-card border border-warning/40 bg-warning/5 px-5 py-10 text-center">
							<AlertTriangle className="mx-auto h-6 w-6 text-warning" />
							<p className="mt-2 text-sm font-medium text-warning">历史样本不足, 无法标定波动率档位</p>
							<p className="mt-1 text-xs text-secondary">{d.insufficient_reason}</p>
							<p className="mt-2 text-[11px] text-muted">
								需要至少 {d.min_sample_days} 个交易日, 当前本地仅 {d.sample_days} 个。
								到「数据」页同步港股指数日K(或调 <code>POST /api/hk/sync_index_daily</code> 带
								<code> count</code>)加深历史后重试。
							</p>
							{/* 客观事实照常给出: 样本不足限制的是分位/档位, 不是最新点位 */}
							{d.level != null && (
								<p className="mt-3 text-xs text-secondary">
									最新点位 <span className="font-mono text-foreground">{fmtLevel(d.level)}</span>
									{d.as_of ? <span className="text-muted"> · {d.as_of}</span> : null}
									{d.period_high != null && d.period_high_date ? (
										<span className="text-muted">
											{' '}· 样本内最高 {fmtLevel(d.period_high)} ({d.period_high_date})
										</span>
									) : null}
								</p>
							)}
						</div>
					) : (
						<>
							{/* 当前档位 + 核心指标 */}
							<section className="rounded-card border border-border bg-surface p-4">
								<div className="flex flex-wrap items-center gap-4">
									<div>
										<div className="text-[10px] text-muted">当前档位</div>
										<div className="mt-1 flex items-baseline gap-2">
											<span
												className={`rounded-btn border px-2.5 py-1 text-lg font-semibold ${style.chip}`}
												data-testid="regime-chip"
											>
												{d.regime_label ?? NA}
											</span>
											<span className="font-mono text-[11px] text-muted">
												分位 {d.percentile == null ? NA : d.percentile.toFixed(1)}
											</span>
										</div>
									</div>
									<div className="h-10 w-px bg-border" />
									<div>
										<div className="text-[10px] text-muted">
											VHSI 点位{d.as_of ? ` · ${d.as_of}` : ''}
										</div>
										<div className="mt-1 flex items-baseline gap-2">
											<span className="font-mono text-2xl font-medium text-foreground">
												{fmtLevel(d.level)}
											</span>
											<span className={`font-mono text-xs ${pctColor(d.change_pct_1d)}`}>
												{d.change_1d == null ? NA : `${d.change_1d > 0 ? '+' : ''}${d.change_1d.toFixed(2)}`}
												{' · '}
												{fmtPct(d.change_pct_1d)}
											</span>
										</div>
									</div>
								</div>
							</section>

							<section className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
								<Stat
									label="连续处于本档"
									value={d.streak_days == null ? NA : `${d.streak_days}`}
									suffix={d.streak_days == null ? undefined : '天'}
									hint="按同一套全样本分位阈值逐日回判, 从最新一天往前数连续落在当前档位的天数(含当日)。"
								/>
								<Stat
									label={`与恒指相关性(近 ${d.corr_window} 日)`}
									value={fmtCorr(d.corr_hsi)}
									valueClass={corrColor(d.corr_hsi)}
									hint={
										d.corr_hsi == null
											? '「—」表示基准(HSI)本地缺失、重叠样本不足或方差为 0。刻意不给 0: 0 会被读成「实测不相关」。'
											: 'VHSI 与 HSI **日收益**的皮尔逊相关系数。正常应显著为负(股跌→波涨); 转正意味着齐涨齐跌, 是结构性异常。'
									}
								/>
								<Stat
									label="MA5"
									value={fmtLevel(d.moving_averages.ma5)}
									hint="5 日均线。样本不足 5 根时为「—」, 不用短窗口凑。"
								/>
								<Stat
									label="MA20"
									value={fmtLevel(d.moving_averages.ma20)}
									hint="20 日均线。样本不足 20 根时为「—」。"
								/>
								<Stat
									label="MA60"
									value={fmtLevel(d.moving_averages.ma60)}
									hint="60 日均线。样本不足 60 根时为「—」。"
								/>
								<Stat
									label="样本内最高 / 最低"
									value={`${fmtLevel(d.period_high)} / ${fmtLevel(d.period_low)}`}
									hint={`标定样本(${d.sample_days} 个交易日)内的极值。最高 ${d.period_high_date ?? NA}, 最低 ${d.period_low_date ?? NA}。`}
								/>
							</section>

							{/* 走势图 + 档位分界线 */}
							<section className="rounded-card border border-border bg-surface p-3">
								<div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-muted">
									<Activity className="h-3.5 w-3.5" />
									VHSI 走势(线) 与 20 日均线(虚线) · 横向虚线为**档位分界点位**
									<span className="ml-auto">相关系数样本 {d.corr_samples} 对</span>
								</div>
								<VolatilityChart data={d} />
							</section>

							{/* 分位阈值表: 判定可查证的关键 */}
							<section className="rounded-card border border-border bg-surface p-3">
								<div className="mb-2 text-[11px] text-muted">
									分位阈值表(标定样本 {d.sample_days} 个交易日 · {d.sample_start ?? NA} ~ {d.sample_end ?? NA})
									<span className="ml-2">
										🔴 阈值来自 VHSI **自身历史**, 不是 VIX 的 20/30 惯例
									</span>
								</div>
								<PercentileTable data={d} />
							</section>

							{/* 档位定义 */}
							<section className="rounded-card border border-border bg-surface p-3">
								<div className="mb-2 text-[11px] text-muted">
									档位定义(按**分位**划分, 左闭右开; 边界值归入更高一档)
								</div>
								<div className="flex flex-wrap gap-2">
									{d.bands.map(b => {
										const bs = bandStyle(b.key)
										const lvl = d.band_upper_levels[b.key]
										const active = d.regime === b.key
										return (
											<div
												key={b.key}
												className={`rounded-btn border px-2.5 py-1.5 text-[11px] ${
													active ? bs.chip : 'border-border bg-base text-secondary'
												}`}
												title={
													b.upper_percentile == null
														? `分位 ≥ 上一档上界(无上界)`
														: `分位 < ${b.upper_percentile}`
												}
											>
												<span className="font-medium">{b.label}</span>
												<span className="ml-1.5 font-mono text-muted">
													{b.upper_percentile == null
														? '无上界'
														: `< p${b.upper_percentile}`}
													{lvl != null ? ` (${fmtLevel(lvl)})` : ''}
												</span>
											</div>
										)
									})}
								</div>
							</section>

							<p className="px-1 text-[10px] leading-relaxed text-muted">
								口径: 数据源 <code>{d.source}</code> · 币种字段 {d.currency}(VHSI 实为无量纲波动率点位) ·
								仅用**收盘价**(日内高低不参与) · 档位为**相对**标尺, 按构造样本里恒有约 10% 的日子属「极低波」、
								约 2% 属「恐慌」, 它回答「现在比过去紧张还是宽松」, 不回答「绝对波动率高不高」 ·
								本页不提供任何交易信号。
							</p>
						</>
					)}
				</div>
			</div>
		</div>
	)
}

/** 涨跌色: 波动率上升用红(与 A 股「红涨」一致), 下降用绿。 */
function pctColor(v: number | null | undefined): string {
	if (v == null || Number.isNaN(v) || v === 0) return 'text-muted'
	return v > 0 ? 'text-bull' : 'text-bear'
}

/**
 * 相关系数配色: **负**相关是常态(股跌→波涨), 用中性色; 转正才是异常, 用警示色。
 * 🔴 刻意与 `pctColor` 相反: 这里红/绿表达的是「正常/异常」, 不是「涨/跌」。
 */
function corrColor(v: number | null | undefined): string {
	if (v == null || Number.isNaN(v)) return 'text-muted'
	return v >= 0 ? 'text-warning' : 'text-foreground'
}

function PercentileTable({data}: {data: HkVolatilityResponse}) {
	const entries = useMemo(() => Object.entries(data.percentiles), [data.percentiles])
	if (entries.length === 0) {
		return <div className="px-1 py-4 text-center text-[11px] text-muted">样本不足, 无分位阈值</div>
	}
	return (
		<table className="w-full text-xs">
			<thead className="border-b border-border text-[11px] text-muted">
				<tr>
					{entries.map(([k]) => (
						<th key={k} className="px-2 py-1.5 text-right font-normal">
							{k.toUpperCase()}
						</th>
					))}
				</tr>
			</thead>
			<tbody>
				<tr data-testid="percentile-row">
					{entries.map(([k, v]) => (
						<td key={k} className="px-2 py-1.5 text-right font-mono text-foreground">
							{fmtLevel(v)}
						</td>
					))}
				</tr>
			</tbody>
		</table>
	)
}

/** VHSI 走势 + MA20 + 档位分界横线。跟随亮暗主题重设轴配色。 */
function VolatilityChart({data}: {data: HkVolatilityResponse}) {
	const elRef = useRef<HTMLDivElement>(null)
	const chartRef = useRef<echarts.ECharts | null>(null)
	const theme = useChartTheme()
	const series = data.series

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

		// 档位分界线: 只画有实际上界的档(末档无上界), 标签用档位中文名。
		const markLines = data.bands
			.filter(b => {
				const lvl = data.band_upper_levels[b.key]
				return b.upper_percentile != null && lvl != null
			})
			.map(b => ({
				yAxis: data.band_upper_levels[b.key] as number,
				lineStyle: {color: bandStyle(b.key).hex, type: 'dashed' as const, width: 1, opacity: 0.55},
				label: {
					formatter: `${b.label} ${fmtLevel(data.band_upper_levels[b.key])}`,
					color: bandStyle(b.key).hex,
					fontSize: 9,
					position: 'insideEndTop' as const,
				},
			}))

		chart.setOption(
			{
				grid: {left: 52, right: 16, top: 28, bottom: 44},
				tooltip: {
					trigger: 'axis',
					backgroundColor: theme.tooltipBg,
					borderColor: theme.tooltipBorder,
					textStyle: {color: theme.tooltipText, fontSize: 11},
					// 🔴 原样输出点位, 不 ×100
					valueFormatter: (v: unknown) => (typeof v === 'number' ? v.toFixed(2) : NA),
				},
				dataZoom: [
					{type: 'inside', start: 0, end: 100},
					{
						type: 'slider',
						height: 16,
						bottom: 8,
						start: 0,
						end: 100,
						fillerColor: theme.zoomFill,
						borderColor: theme.border,
						textStyle: {color: theme.text, fontSize: 9},
					},
				],
				xAxis: {
					type: 'category',
					data: series.map((p: HkVolatilityPoint) => p.date),
					axisLine: {lineStyle: {color: theme.border}},
					axisLabel: {color: theme.text, fontSize: 10},
				},
				yAxis: {
					type: 'value',
					name: 'VHSI',
					scale: true,
					nameTextStyle: {color: theme.text, fontSize: 10},
					axisLabel: {color: theme.text, fontSize: 10},
					splitLine: {lineStyle: {color: theme.grid}},
				},
				series: [
					{
						name: 'VHSI',
						type: 'line',
						data: series.map(p => p.close),
						showSymbol: false,
						connectNulls: false,
						lineStyle: {color: theme.textStrong, width: 1.6},
						markLine: {
							symbol: 'none',
							silent: true,
							data: markLines,
						},
					},
					{
						name: 'MA20',
						type: 'line',
						data: series.map(p => p.ma20),
						showSymbol: false,
						connectNulls: false,
						lineStyle: {color: '#F79009', width: 1.2, type: 'dashed'},
					},
				],
				legend: {
					top: 0,
					right: 0,
					textStyle: {color: theme.text, fontSize: 10},
					itemWidth: 14,
					data: ['VHSI', 'MA20'],
				},
			},
			{notMerge: true},
		)
	}, [series, data.bands, data.band_upper_levels, theme])

	return (
		<div
			ref={elRef}
			className="h-72 w-full"
			role="img"
			aria-label="VHSI 走势与档位分界线"
			data-testid="volatility-chart"
		/>
	)
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
