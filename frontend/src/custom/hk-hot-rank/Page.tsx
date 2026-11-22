import {useEffect, useMemo, useRef, useState} from 'react'
import {useQuery} from '@tanstack/react-query'
import * as echarts from 'echarts'
import {AlertTriangle, ArrowLeft, ExternalLink, Flame, RefreshCw, Search} from 'lucide-react'
import {api, type HkHotRankItem, type HkHotRankPoint} from '@/lib/api'
import {QK} from '@/lib/queryKeys'
import {PageHeader} from '@/components/PageHeader'
import {Skeleton} from '@/components/data/Skeleton'
import {useChartTheme} from '@/lib/theme'
import {openStockPreview} from '@/extensions/stockPreview'

/**
 * 港股人气榜 — 东方财富股吧个股人气榜(前 100 名)。
 *
 * 🔴🔴 改这个文件前先读 `HkHotRankResponse` / `HkHotRankDetailResponse` 的注释,
 * 四条口径约束:
 *
 * 1. **名次方向与涨跌相反**: `rank` 小 = 靠前 = 更热门。故排名走势图的 y 轴
 *    **必须** `inverse: true` —— 不反转的话「越来越热门」会画成一条向下的线,
 *    而那条线在所有人的直觉里都是「变差」。
 * 2. **本页没有任何价格/涨跌幅**, 这是刻意的: 价格属于**行情**维度, 已由看盘/
 *    自选/详情走 `quoteService`(带缓存与新鲜度判定)提供。这里再给一份就会让
 *    同一只股票出现**两个价格口径**, 用户无法判断哪个对(CONTRIBUTING §4)。
 *    要看价格请用行末的「详情」按钮打开个股弹窗(`openStockPreview`)。
 * 3. **排名必须有分母**: `market_all_count`(实测 2811)拿不到时, `rank_percentile`
 *    全为 null, 界面必须显示「—」而**不是** `0%` —— 0% 会被读成「全市场第一」。
 * 4. **两种时间格式刻意不统一**: 日频历史是日期(`2026-07-26`), 当日盘中是时刻
 *    (`2026-11-22 00:00:00`)。轴标签可按各自格式裁剪显示, 但 tooltip 必须给出
 *    **原始** `calc_time`, 否则就等于替上游编了一个它没说过的精度(§13)。
 *
 * 🔴 `calc_time` 是**人气榜重算时刻**(实测 10 分钟一步), 不是行情时刻, 也**不是
 * 交易日** —— 实测非交易日(周日)仍有值, 不可当「最新交易日」展示。
 */

const TITLE = '港股人气榜'

/** 每次 miss 都打上游, 且上游 10 分钟才重算一次 ⇒ staleTime 给足。 */
const STALE_MS = 300_000

/** 空值占位。🔴 不能换成 `0`: 名次/分位的 0 都有明确含义, 不是「没有」。 */
const NA = '—'

/** 名次变动配色: **刻意不用** `text-bull`/`text-bear`。
 *
 *  🔴 本项目红绿是**涨跌**语义(红涨绿跌)。人气「升温」若用红色, 会被读成
 *  「这只股票在涨」—— 而本页根本不提供价格, 那个推断无从验证。故走中性/强调色。 */
const DELTA_UP = 'text-accent'
const DELTA_DOWN = 'text-muted'

function fmtInt(v: number | null | undefined): string {
	return v == null || Number.isNaN(v) ? NA : String(v)
}

/**
 * 「前 X%」展示。
 *
 * 🔴 null 必须渲染成「—」: 分母(`market_all_count`)缺失时后端给的是 null,
 * 渲染成 `0%` 会把「算不出来」说成「全市场第一名」。
 */
function fmtPercentile(v: number | null | undefined): string {
	if (v == null || Number.isNaN(v)) return NA
	if (v < 1) return `${v.toFixed(2)}%`
	if (v < 10) return `${v.toFixed(1)}%`
	return `${v.toFixed(0)}%`
}

/**
 * 轴标签: 日频序列取 `MM-DD`, 盘中序列取 `HH:mm`。
 *
 * 🔴 这是**显示裁剪**而非口径转换 —— 原始 `calc_time` 完整保留在 tooltip 里。
 * 两种格式刻意不统一(见 `HkHotRankPoint`), 故按各自格式取可读部分, 不互相补位。
 */
function axisLabel(calcTime: string): string {
	const sp = calcTime.indexOf(' ')
	if (sp < 0) {
		const dash = calcTime.indexOf('-')
		return dash > 0 ? calcTime.slice(dash + 1) : calcTime
	}
	return calcTime.slice(sp + 1, sp + 6)
}

/** 名次变动: **正 = 名次上升**(数字变小)。0 与 null 分开渲染。 */
function RankDelta({value, title}: {value: number | null; title: string}) {
	if (value == null) return <span className="font-mono text-[11px] text-muted">{NA}</span>
	if (value === 0) {
		return <span className="font-mono text-[11px] text-muted" title={title}>0</span>
	}
	const up = value > 0
	return (
		<span
			className={`inline-flex items-center gap-0.5 font-mono text-[11px] ${up ? DELTA_UP : DELTA_DOWN}`}
			title={`${title} · ${up ? '名次上升' : '名次下降'}`}
			data-testid={up ? 'delta-up' : 'delta-down'}
		>
			{up ? '↑' : '↓'}{Math.abs(value)}
		</span>
	)
}

function Stat({label, value, hint, valueClass}: {
	label: string
	value: string
	hint?: string
	valueClass?: string
}) {
	return (
		<div title={hint} className="rounded-card border border-border bg-surface px-3 py-2">
			<div className="truncate text-[10px] text-muted">{label}</div>
			<div className={`mt-0.5 font-mono text-base font-medium ${valueClass ?? 'text-foreground'}`}>
				{value}
			</div>
		</div>
	)
}

export function HkHotRank() {
	const [selected, setSelected] = useState<string | null>(null)
	const [filter, setFilter] = useState('')

	const query = useQuery({
		queryKey: QK.hkHotRank,
		queryFn: () => api.hkHotRank(),
		staleTime: STALE_MS,
		retry: false,
	})

	const d = query.data

	// 客户端过滤: 榜单只有 100 行, 不值得为它加一个后端查询参数。
	const rows = useMemo(() => {
		const items = d?.items ?? []
		const q = filter.trim().toLowerCase()
		if (!q) return items
		return items.filter(it =>
			it.code.toLowerCase().includes(q) ||
			it.symbol.toLowerCase().includes(q) ||
			(it.name ?? '').toLowerCase().includes(q))
	}, [d?.items, filter])

	return (
		<div className="flex h-full flex-col">
			<PageHeader
				title={TITLE}
				subtitle="东方财富股吧个股人气榜 · 前 100 名(上游硬上限, 不可翻页) · 本页不含价格, 看行情请开个股详情"
				right={
					<button
						type="button"
						onClick={() => void query.refetch()}
						disabled={query.isFetching}
						title="重新拉取上游榜单"
						className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border bg-surface/80 px-3 text-xs text-secondary transition-colors hover:text-foreground disabled:opacity-50"
					>
						<RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? 'animate-spin' : ''}`} />
						刷新
					</button>
				}
			/>
			<div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
				<div className="mx-auto max-w-7xl space-y-4">
					{query.error ? (
						/* 🔴 上游失败 → 后端给 502, 这里必须显式报错。
						   绝不能降级成空榜单: 「前 100 名一个都没有」会被读成
						   「全市场无人关注」, 那是把故障伪装成结论。 */
						<div className="rounded-card border border-danger/40 bg-danger/5 px-5 py-10 text-center text-sm text-danger">
							<AlertTriangle className="mx-auto h-6 w-6" />
							<p className="mt-2 font-medium">人气榜加载失败</p>
							<p className="mt-1 text-xs text-secondary">{(query.error as Error).message}</p>
							<p className="mt-2 text-[11px] text-muted">
								上游为东方财富股吧榜单接口, 可能限流或暂时不可用; 稍后点「刷新」重试。
							</p>
						</div>
					) : query.isLoading ? (
						<div className="space-y-2">
							<Skeleton h="h-20" />
							<Skeleton h="h-96" />
						</div>
					) : !d ? (
						<div className="rounded-card border border-dashed border-border px-5 py-10 text-center text-sm text-muted">
							暂无数据
						</div>
					) : (
						<>
							<section className="grid grid-cols-2 gap-2 lg:grid-cols-4">
								<Stat
									label="榜单条数"
									value={`${d.total} / ${d.max_count}`}
									hint="上游对 pageSize>100 或 pageNo=2 静默返回空列表, 故本端点恒为前 100 名, 不可翻页"
								/>
								<Stat
									label="全市场参与排名标的数(分母)"
									value={fmtInt(d.market_all_count)}
									valueClass={d.market_all_count == null ? 'text-warning' : undefined}
									hint="排名的分母。没有它, 「第 100 名」无法解读: 在 100 个标的里是垫底, 在 2811 个里是前 3.6%"
								/>
								<Stat
									label="榜单重算时刻"
									value={d.calc_time ?? NA}
									hint="上游人气榜的重算时刻(实测约 10 分钟一步)。不是行情时刻, 也不是交易日 —— 非交易日同样有值"
								/>
								<Stat
									label="名称来源"
									value={d.names_synced ? '本地维表已同步' : '维表未同步'}
									valueClass={d.names_synced ? undefined : 'text-warning'}
									hint="名称取自本地 instruments_hk 维表(上游榜单只给代码)。未同步时名称为空, 但榜单本身有效"
								/>
							</section>

							{!d.names_synced && (
								/* 🔴 独立于错误态与空态: 榜单是好的, 只是名称缺失。
								   不说明的话用户会看到一整列空白, 以为没取到数据。 */
								<div className="flex items-start gap-2 rounded-card border border-warning/40 bg-warning/5 px-3 py-2 text-[11px] text-warning">
									<AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
									<span data-testid="names-unsynced">
										维表未同步, 名称暂缺 —— 榜单名次与分位仍然有效。到「数据」页同步港股维表后刷新即可显示名称。
									</span>
								</div>
							)}

							{d.market_all_count == null && (
								<div className="flex items-start gap-2 rounded-card border border-warning/40 bg-warning/5 px-3 py-2 text-[11px] text-warning">
									<AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
									<span data-testid="no-denominator">
										未取到全市场标的数, 「前 X%」一列不可用 —— 名次本身仍然有效, 但无法换算成市场分位。
									</span>
								</div>
							)}

							{selected ? (
								<HotRankDetail symbol={selected} onBack={() => setSelected(null)} />
							) : (
								<section className="rounded-card border border-border bg-surface">
									<div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
										<Flame className="h-4 w-4 shrink-0 text-muted" />
										<span className="text-[11px] text-muted">
											前 {d.total} 名 · 点任意一行看该名次的历史走势
										</span>
										<div className="ml-auto flex items-center gap-1.5 rounded-btn border border-border bg-base px-2 py-1">
											<Search className="h-3 w-3 shrink-0 text-muted" />
											<input
												value={filter}
												onChange={e => setFilter(e.target.value)}
												placeholder="过滤代码/名称"
												aria-label="过滤代码或名称"
												className="w-32 bg-transparent text-[11px] text-foreground outline-none placeholder:text-muted"
											/>
										</div>
									</div>
									<HotRankTable
										rows={rows}
										namesSynced={d.names_synced}
										onPick={setSelected}
									/>
								</section>
							)}

							<p className="px-1 text-[10px] leading-relaxed text-muted">
								口径: 数据源 <code>{d.source}</code> · 榜单为东方财富股吧**个股人气榜**, 与 A 股侧
								同花顺热度是**两套独立体系**, 不可互相比较或混排 · 名次小 = 更热门 ·
								「短周期/长周期变动」是上游给的两个不同窗口的名次变动, **上游未文档化具体窗口**,
								故原样透传而不改名为「日变动/周变动」(正数 = 名次上升) ·
								本页不提供价格与涨跌幅, 也不提供任何交易信号。
							</p>
						</>
					)}
				</div>
			</div>
		</div>
	)
}

function HotRankTable({rows, namesSynced, onPick}: {
	rows: HkHotRankItem[]
	namesSynced: boolean
	onPick: (symbol: string) => void
}) {
	if (rows.length === 0) {
		return (
			<div className="px-3 py-10 text-center text-xs text-muted" data-testid="table-empty">
				没有匹配的标的
			</div>
		)
	}
	return (
		<div className="overflow-x-auto">
			<table className="w-full text-xs">
				<thead className="border-b border-border text-[11px] text-muted">
					<tr>
						<th className="px-3 py-2 text-right font-normal">名次</th>
						<th className="px-3 py-2 text-left font-normal">代码</th>
						<th className="px-3 py-2 text-left font-normal">名称</th>
						<th className="px-3 py-2 text-right font-normal" title="上游未文档化具体窗口; 正 = 名次上升">
							短周期变动
						</th>
						<th className="px-3 py-2 text-right font-normal" title="上游未文档化具体窗口; 正 = 名次上升">
							长周期变动
						</th>
						<th className="px-3 py-2 text-right font-normal" title="rank / 全市场标的数 × 100, 越小越热门">
							市场分位
						</th>
						<th className="px-3 py-2 text-right font-normal">行情</th>
					</tr>
				</thead>
				<tbody>
					{rows.map(it => (
						<tr
							key={it.symbol}
							data-testid="rank-row"
							data-symbol={it.symbol}
							onClick={() => onPick(it.symbol)}
							className="cursor-pointer border-b border-border/50 transition-colors last:border-0 hover:bg-base/60"
						>
							<td className="px-3 py-1.5 text-right font-mono font-medium text-foreground">
								{it.rank}
							</td>
							<td className="px-3 py-1.5 font-mono text-secondary">{it.code}</td>
							<td className="px-3 py-1.5 text-foreground">
								{it.name ?? (
									<span className="text-muted" title={namesSynced ? '该标的不在本地维表里' : '维表未同步'}>
										{NA}
									</span>
								)}
							</td>
							<td className="px-3 py-1.5 text-right">
								<RankDelta value={it.rank_change} title="短周期名次变动" />
							</td>
							<td className="px-3 py-1.5 text-right">
								<RankDelta value={it.his_rank_change} title="长周期名次变动" />
							</td>
							<td className="px-3 py-1.5 text-right font-mono text-secondary">
								{fmtPercentile(it.rank_percentile)}
							</td>
							<td className="px-3 py-1.5 text-right">
								{/* 🔴 本页刻意不给价格(会与看盘/详情形成两个口径),
								   但用户确实需要看行情 ⇒ 用宿主弹窗打开个股详情,
								    价格仍由 quoteService 单一来源提供。 */}
								<button
									type="button"
									title="打开个股详情看行情"
									aria-label={`打开 ${it.symbol} 详情`}
									onClick={e => {
										e.stopPropagation()
										openStockPreview({symbol: it.symbol, name: it.name})
									}}
									className="inline-flex h-6 items-center gap-1 rounded-btn border border-border bg-base px-1.5 text-[10px] text-secondary transition-colors hover:text-foreground"
								>
									<ExternalLink className="h-3 w-3" />
									详情
								</button>
							</td>
						</tr>
					))}
				</tbody>
			</table>
		</div>
	)
}

function HotRankDetail({symbol, onBack}: {symbol: string; onBack: () => void}) {
	const query = useQuery({
		queryKey: QK.hkHotRankDetail(symbol, true),
		queryFn: () => api.hkHotRankDetail(symbol, true),
		staleTime: STALE_MS,
		retry: false,
	})
	const d = query.data

	return (
		<section className="space-y-3">
			<div className="flex flex-wrap items-center gap-2">
				<button
					type="button"
					onClick={onBack}
					className="inline-flex h-7 items-center gap-1 rounded-btn border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground"
				>
					<ArrowLeft className="h-3 w-3" />
					返回榜单
				</button>
				<span className="font-mono text-xs text-foreground">{symbol}</span>
				{d?.name ? <span className="text-xs text-secondary">{d.name}</span> : null}
				<button
					type="button"
					title="打开个股详情看行情"
					onClick={() => openStockPreview({symbol, name: d?.name ?? null})}
					className="ml-auto inline-flex h-7 items-center gap-1 rounded-btn border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground"
				>
					<ExternalLink className="h-3 w-3" />
					个股详情(含行情)
				</button>
			</div>

			{query.error ? (
				<div className="rounded-card border border-danger/40 bg-danger/5 px-5 py-8 text-center text-sm text-danger">
					加载失败 · {(query.error as Error).message}
				</div>
			) : query.isLoading ? (
				<div className="space-y-2">
					<Skeleton h="h-16" />
					<Skeleton h="h-64" />
				</div>
			) : !d ? (
				<div className="rounded-card border border-dashed border-border px-5 py-8 text-center text-sm text-muted">
					暂无数据
				</div>
			) : !d.found ? (
				/* 🔴 榜外标的是**合法答案**(上游返回 200 + 全 null), 不是错误。
				   渲染成「加载失败」会让人以为接口坏了。 */
				<div className="rounded-card border border-dashed border-border px-5 py-8 text-center">
					<p className="text-sm text-secondary" data-testid="not-found">
						该标的暂不在人气榜体系内
					</p>
					<p className="mt-1 text-[11px] text-muted">
						上游未收录 <code>{symbol}</code> 的人气排名(可能是新上市、已退市或不在统计范围)。
						这不是接口故障 —— 榜单本身正常。
					</p>
				</div>
			) : (
				<>
					<div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
						<Stat
							label="当前名次"
							value={fmtInt(d.rank)}
							hint="小 = 更热门"
						/>
						<Stat
							label="市场分位"
							value={fmtPercentile(d.rank_percentile)}
							hint={
								d.market_all_count == null
									? '未取到全市场标的数(分母), 无法换算分位'
									: `名次 / 全市场 ${d.market_all_count} 个标的 × 100`
							}
						/>
						<Stat
							label="短周期变动"
							value={d.rank_change == null ? NA : `${d.rank_change > 0 ? '+' : ''}${d.rank_change}`}
							hint="正 = 名次上升。上游未文档化具体窗口"
						/>
						<Stat
							label="长周期变动"
							value={d.his_rank_change == null ? NA : `${d.his_rank_change > 0 ? '+' : ''}${d.his_rank_change}`}
							hint="正 = 名次上升。与短周期是两个不同窗口"
						/>
					</div>

					<div className="rounded-card border border-border bg-surface p-3">
						<div className="mb-1 flex flex-wrap items-baseline gap-2">
							<span className="text-[11px] text-muted">
								日频名次走势 · {d.history_days} 个交易日
							</span>
							<span className="text-[10px] text-muted">
								(y 轴已反转: 线**往上**走 = 名次数字变小 = 更热门)
							</span>
						</div>
						<RankChart
							points={d.history}
							testId="history-chart"
							ariaLabel="日频人气名次走势"
							emptyText="无日频历史"
						/>
					</div>

					<div className="rounded-card border border-border bg-surface p-3">
						<div className="mb-1 flex flex-wrap items-baseline gap-2">
							<span className="text-[11px] text-muted">
								当日盘中名次 · {d.intraday_points} 个点(10 分钟粒度)
							</span>
							<span className="text-[10px] text-muted">
								(y 轴已反转: 线**往上**走 = 更热门)
							</span>
						</div>
						<RankChart
							points={d.intraday}
							testId="intraday-chart"
							ariaLabel="当日盘中人气名次走势"
							emptyText="无当日盘中数据(可能非交易时段)"
						/>
					</div>

					<p className="px-1 text-[10px] leading-relaxed text-muted">
						口径: 数据源 <code>{d.source}</code> · 榜单重算时刻 {d.calc_time ?? NA}
						(非行情时刻, 也非交易日) · 日频历史与当日盘中是**上游维护的滚动窗口**,
						本项目不落库, 每次读取都取上游当前值 · 历史序列的 `calc_time` 是日期、
						盘中是时刻, 两种格式刻意不统一(强行统一会给日频补一个假的 00:00:00)。
					</p>
				</>
			)}
		</section>
	)
}

/**
 * 名次走势图。
 *
 * 🔴🔴 **y 轴必须 `inverse: true`**: 名次是「小 = 好」, 与价格/成交额那类
 * 「大 = 好」的序列方向相反。不反转的话「人气持续升温」会画成一条一路向下的线,
 * 而下降线在所有人的直觉里都等于「变差」—— 图会系统性地传达反的结论。
 */
function RankChart({points, testId, ariaLabel, emptyText}: {
	points: HkHotRankPoint[]
	testId: string
	ariaLabel: string
	emptyText: string
}) {
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
		if (!chart || points.length === 0) return
		chart.setOption(
			{
				grid: {left: 48, right: 16, top: 16, bottom: 40},
				tooltip: {
					trigger: 'axis',
					backgroundColor: theme.tooltipBg,
					borderColor: theme.tooltipBorder,
					textStyle: {color: theme.tooltipText, fontSize: 11},
					// 🔴 tooltip 给**原始** calc_time: 轴标签为了可读做了裁剪,
					// 这里再裁一次就等于替上游编了一个它没说过的时间精度。
					formatter: (params: unknown) => {
						const arr = params as Array<{dataIndex: number; value: number | null}>
						const p = Array.isArray(arr) ? arr[0] : arr
						const raw = points[p.dataIndex]?.calc_time ?? NA
						const rank = p.value
						return `${raw}<br/>名次 ${rank == null ? NA : rank}`
					},
				},
				dataZoom: [
					{type: 'inside', start: 0, end: 100},
					{
						type: 'slider',
						height: 16,
						bottom: 6,
						start: 0,
						end: 100,
						fillerColor: theme.zoomFill,
						borderColor: theme.border,
						textStyle: {color: theme.text, fontSize: 9},
					},
				],
				xAxis: {
					type: 'category',
					data: points.map(p => axisLabel(p.calc_time)),
					axisLine: {lineStyle: {color: theme.border}},
					axisLabel: {color: theme.text, fontSize: 10},
				},
				yAxis: {
					type: 'value',
					name: '名次',
					// 🔴 见函数 docstring: 不反转就会把「升温」画成「下滑」。
					inverse: true,
					minInterval: 1,
					nameTextStyle: {color: theme.text, fontSize: 10},
					axisLabel: {color: theme.text, fontSize: 10},
					splitLine: {lineStyle: {color: theme.grid}},
				},
				series: [
					{
						name: '名次',
						type: 'line',
						data: points.map(p => p.rank),
						showSymbol: false,
						connectNulls: false,
						lineStyle: {color: theme.textStrong, width: 1.6},
					},
				],
			},
			{notMerge: true},
		)
	}, [points, theme])

	if (points.length === 0) {
		return (
			<div className="py-8 text-center text-[11px] text-muted" data-testid={`${testId}-empty`}>
				{emptyText}
			</div>
		)
	}
	return <div ref={elRef} className="h-56 w-full" role="img" aria-label={ariaLabel} data-testid={testId} />
}
