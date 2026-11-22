/**
 * 行业估值温度计 (PR6 · 方案 §10 恒生行业四件套第 2 件)。
 *
 * 按恒生行业聚合的 **TTM 股息率 × 派息覆盖率**, 默认按中位数降序 —— 方案原话是
 * 「按股息率降序」用于**价值洼地识别**, 因为「港股是红利市场, 这个比涨幅排名
 * 更有用」。
 *
 * 🔴 三条口径必须在 UI 上可见, 藏起来就等于把已知缺陷伪装成完整数据 (§4):
 *
 * 1. **中位数只在派息者上算**, 覆盖率与之并列。实测全市场派息覆盖率仅 36.8%,
 *    若把不派息的计成 0, 31 个行业里 15 个以上的中位数恰好是 `0.0`, 排序后第一屏
 *    全是并列的零。所以中位数答「愿意派息的公司有多大方」, 覆盖率答「有多少公司
 *    真的派息」—— **两个数一起看才成立**, 页脚把这句话写给用户。
 * 2. **均值受仙股一次性派息扭曲** (实测专业零售 均值 23.6% vs 中位数 8.1%),
 *    故表头标注「参考」, 且**不参与默认排序**。
 * 3. **PE 缺源明示**。后端下发 `pe_available: false` + 理由, 这里渲染成一条中性
 *    说明而不是留一个空列 —— 空列会被读成「这个行业 PE 为空」, 而真相是「我们
 *    没有源」。用中性色不用 warning 黄: 这不是故障, 是一个稳定的事实。
 *
 * 🔴 `excluded_count` 逐行展示: 那是「有行业但算不出股息率」的成分数 (无收盘价 /
 * 派息金额未能解析)。它们**不进任何统计**也**绝不当成 0%** —— 把「派了但读不出
 * 多少」说成「没派」是看不出来的伪造 (§13)。
 *
 * 单位: 后端给的 `*_pct` 已经是**百分比数值** (`5.0` = `5.0%`), 故这里用本地
 * `fmtPctValue` 而**不是** `lib/format.ts` 的 `fmtPct` —— 后者会再乘 100 并加
 * `+` 号, 是给小数涨跌幅用的 (CONTRIBUTING §3.1: 换算只在数据源边界做一次)。
 */
import { useMemo, useState } from 'react'
import { Gauge, Info, X } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api, type HkSectorValuationRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { type Market } from '@/lib/market'
import { marketContent } from '@/lib/marketContent'
import { Modal } from '@/components/Modal'

interface Props {
	onClose: () => void
	/**
	 * 🔴 必填: 端点是港股专属 (`/api/hk/*`), 且入口渲染与否由
	 * `lib/board-market-support.ts` 的 `SECTOR_VALUATION_THERMOMETER` 表决定。
	 * 这里仍要求显式传入而不是内部 `useMarket()`, 与 `RpsRotationDialog` 同口径:
	 * 展示层不该自己兜底构造查询参数 (`lib/market.ts` 头注)。
	 */
	market: Market
}

/** TTM 窗口 (自然日)。与后端 `HK_SECTOR_VALUATION_DEFAULT_DAYS` 同值。 */
const DAYS = 365

/**
 * 数据**不落库**, 每次 miss 都打一次上游报表 (一年窗口约 8 页)。给足 staleTime
 * 挡住「反复开关对话框」造成的重复上游请求。
 */
const STALE_MS = 10 * 60 * 1000

/** 中位数高于此值视为「高股息」, 用强调色 (与港股分红页的 5% 阈值一致)。 */
const HIGH_YIELD_PCT = 5

type SortKey = 'median_yield_pct' | 'coverage_pct' | 'avg_yield_pct' | 'member_count'

/**
 * 百分比数值格式化。🔴 **不乘 100、不加 `+` 号** —— 后端已完成换算。
 * `null` 渲染成「—」而不是 `0.00%`: 前者是「算不出」, 后者是「查证过为零」。
 */
function fmtPctValue(v: number | null | undefined, digits = 2): string {
	if (v == null || !Number.isFinite(v)) return '—'
	return `${v.toFixed(digits)}%`
}

function todayIso(): string {
	const d = new Date()
	const p = (n: number) => String(n).padStart(2, '0')
	return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

export function SectorValuationDialog({ onClose, market }: Props) {
	const marketLabel = marketContent(market).label
	const end = useMemo(todayIso, [])
	const [sortKey, setSortKey] = useState<SortKey>('median_yield_pct')
	// 缺省与后端一致: 中位数降序。改排序键时沿用当前方向, 只有点同一个键才翻转。
	const [sortDir, setSortDir] = useState<1 | -1>(-1)

	const query = useQuery({
		queryKey: QK.hkSectorValuation(DAYS, end),
		queryFn: () => api.hkSectorValuation({ days: DAYS, end }),
		staleTime: STALE_MS,
	})
	const data = query.data

	const rows = useMemo(() => {
		const items = data?.items ?? []
		// null 一律排最后: 「算不出」不该因为排序而冒充成「最低」
		return [...items].sort((a, b) => cmpNullLast(a[sortKey], b[sortKey], sortDir))
	}, [data, sortKey, sortDir])

	function toggleSort(key: SortKey) {
		if (key === sortKey) {
			setSortDir(d => (d === -1 ? 1 : -1))
		} else {
			setSortKey(key)
			// 覆盖率与成分数天然「大的先看」; 均值/中位数也是高的先看
			setSortDir(-1)
		}
	}

	const overallCoverage =
		data && data.priced_total > 0 ? (data.payer_total / data.priced_total) * 100 : null

	return (
		<Modal
			onClose={onClose}
			labelledBy="sector-valuation-title"
			overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
			panelClassName="w-[94vw] max-w-[1080px] h-[88vh] bg-surface border border-border rounded-card shadow-xl flex flex-col"
		>
			{/* 标题栏 */}
			<div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-2.5">
				<div className="flex items-center gap-2">
					<Gauge className="h-4 w-4 text-emerald-400" />
					<span id="sector-valuation-title" className="text-sm font-medium text-foreground">
						{marketLabel}行业估值温度计
					</span>
					{data && (
						<span className="text-[11px] text-muted" data-testid="sv-window">
							{data.window_start} ~ {data.window_end} · {data.days} 天 TTM · {data.total} 个行业
						</span>
					)}
				</div>
				<button
					onClick={onClose}
					className="rounded-btn p-1 text-muted hover:bg-base hover:text-foreground"
					title="关闭"
					aria-label="关闭"
				>
					<X className="h-4 w-4" />
				</button>
			</div>

			<div className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
				{/* 🔴 PE 缺源明示。中性色(不是 warning 黄): 这不是故障, 是一个稳定的事实。
				    文案来自后端 —— 缺源理由是数据源侧知识, 前端不该自己编一份, 否则将来
				    接上 PE 时会有两处说法不一致。 */}
				{data && data.pe_available === false && (
					<div
						data-testid="sv-pe-unavailable"
						className="flex items-start gap-2 rounded-card border border-border bg-base/60 px-3 py-2 text-[11px] leading-relaxed text-secondary"
					>
						<Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" />
						<span>
							<strong className="font-medium text-foreground">本表不含 PE。</strong>
							{data.pe_unavailable_reason}
						</span>
					</div>
				)}

				{/* 全市场口径: 让「是不是全市场」可被肉眼审计 (方案 §10 警告过 QuantMind
				    早期误用 141 只港股通子集而返工)。 */}
				{data && (
					<div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted" data-testid="sv-meta">
						<span>
							成分 <strong className="font-medium text-foreground">{data.member_total}</strong> 只
						</span>
						<span>
							有行情 <strong className="font-medium text-foreground">{data.priced_total}</strong>
						</span>
						<span>
							近一年派息 <strong className="font-medium text-emerald-400">{data.payer_total}</strong>
						</span>
						<span>
							全市场覆盖率{' '}
							<strong className="font-medium text-foreground">{fmtPctValue(overallCoverage, 1)}</strong>
						</span>
						<span>分红事件 {data.event_count} 条</span>
						{data.excluded_total > 0 && (
							<span title="无收盘价或派息金额未能解析, 不进任何统计, 也不当成 0%">
								算不出 <strong className="font-medium text-foreground">{data.excluded_total}</strong>
							</span>
						)}
						{data.unclassified_count > 0 && (
							<span title="维表里行业为空的成分(债券残留)。不造「未知」假行业, 单独计数">
								未分类 {data.unclassified_count}
							</span>
						)}
					</div>
				)}

				{query.isLoading && (
					<div className="py-16 text-center text-xs text-muted" data-testid="sv-loading">
						正在按除净日窗口拉取全市场分红并聚合…
					</div>
				)}

				{query.isError && (
					<div
						data-testid="sv-error"
						className="rounded-card border border-warning/40 bg-warning/10 px-3 py-2 text-[11px] text-warning"
					>
						{/* 502 = 上游报表不可用。不降级成空表: 「拉不到」和「没人派息」是两种状态 */}
						数据源不可用: {query.error instanceof Error ? query.error.message : String(query.error)}
					</div>
				)}

				{data && rows.length === 0 && !query.isLoading && (
					<div className="py-16 text-center text-xs text-muted" data-testid="sv-empty">
						窗口内没有可聚合的行业数据 — 请先在「数据 → 港股数据」完成维表与日K同步
					</div>
				)}

				{rows.length > 0 && (
					<div className="overflow-hidden rounded-card border border-border bg-surface">
						<table className="w-full text-xs">
							<thead className="border-b border-border bg-base/60 text-[11px] text-muted">
								<tr>
									<th className="px-2 py-2 text-left font-normal">行业</th>
									<Th onClick={() => toggleSort('member_count')} active={sortKey === 'member_count'} dir={sortDir} right>
										成分
									</Th>
									<th className="px-2 py-2 text-right font-normal" title="窗口内确有现金派息的成分数 / 能算出股息率的成分数">
										派息
									</th>
									<Th onClick={() => toggleSort('coverage_pct')} active={sortKey === 'coverage_pct'} dir={sortDir} right>
										覆盖率
									</Th>
									<Th onClick={() => toggleSort('median_yield_pct')} active={sortKey === 'median_yield_pct'} dir={sortDir} right>
										中位股息率
									</Th>
									<Th onClick={() => toggleSort('avg_yield_pct')} active={sortKey === 'avg_yield_pct'} dir={sortDir} right>
										均值<span className="text-muted/70">(参考)</span>
									</Th>
									<th className="px-2 py-2 text-right font-normal">最高</th>
									<th className="px-2 py-2 text-left font-normal">龙头派息者</th>
									<th className="px-2 py-2 text-right font-normal" title="有行业但算不出股息率的成分数(无收盘价/金额未能解析)。不进统计, 也不当成 0%">
										算不出
									</th>
								</tr>
							</thead>
							<tbody data-testid="sv-body">
								{rows.map(r => (
									<Row key={r.industry} r={r} />
								))}
							</tbody>
						</table>
					</div>
				)}

				{/* 口径说明: 这三句话是「怎么读这张表」, 不是免责声明。中位数与覆盖率
				    必须一起看, 否则会把「8 家里 3 家派、派得很高」和「200 家里 180 家派」
				    当成同一件事。 */}
				<div className="space-y-1 rounded-card border border-border bg-base/40 px-3 py-2 text-[11px] leading-relaxed text-muted">
					<p>
						<strong className="font-medium text-secondary">中位/均值/最高只在「派息者」上统计</strong>
						, 不派息的成分计入<strong className="font-medium text-secondary">覆盖率</strong>而不计入股息率 ——
						全市场派息覆盖率仅约三成半, 若把不派息的计成 0, 半数行业的中位数会并列成 0.00%, 表就分不出高低了。
						两列一起看: 中位数答「派息的公司有多大方」, 覆盖率答「有多少公司真的派息」。
					</p>
					<p>
						股息率 = 窗口内每股现金派息(港币, 按<strong className="font-medium text-secondary">除净日</strong>归属)
						之和 ÷ 最新<strong className="font-medium text-secondary">不复权</strong>收盘价 × 100。
						用前复权价会把有分红史的标的系统性算高, 故刻意不用。
					</p>
					<p>
						「—」表示算不出来(该行业窗口内无人派息 / 无收盘价 / 派息金额未能解析), 与
						<strong className="font-medium text-secondary"> 0.00%</strong>(查证过、确无现金派息)是两种不同状态。
						「算不出」列的成分不进任何统计, 也绝不当成 0。
					</p>
				</div>
			</div>
		</Modal>
	)
}

function Th({
	onClick,
	active,
	dir,
	right,
	children,
}: {
	onClick: () => void
	active: boolean
	dir: 1 | -1
	right?: boolean
	children: React.ReactNode
}) {
	return (
		<th className={cn('px-2 py-2 font-normal', right ? 'text-right' : 'text-left')}>
			<button
				onClick={onClick}
				className={cn(
					'inline-flex items-center gap-0.5 transition-colors hover:text-foreground',
					active && 'text-accent',
				)}
			>
				{children}
				{active && <span className="text-[9px]">{dir === -1 ? '▼' : '▲'}</span>}
			</button>
		</th>
	)
}

function Row({ r }: { r: HkSectorValuationRow }) {
	const high = r.median_yield_pct != null && r.median_yield_pct >= HIGH_YIELD_PCT
	return (
		<tr className="border-b border-border/50 last:border-0 hover:bg-base/40">
			<td className="max-w-[12rem] truncate px-2 py-1.5 text-foreground" title={r.industry}>
				{r.industry}
			</td>
			<td className="px-2 py-1.5 text-right text-secondary">{r.member_count}</td>
			<td className="px-2 py-1.5 text-right text-secondary">
				{r.payer_count}
				<span className="text-muted">/{r.priced_count}</span>
			</td>
			<td className="px-2 py-1.5 text-right text-secondary">{fmtPctValue(r.coverage_pct, 1)}</td>
			<td
				className={cn(
					'px-2 py-1.5 text-right',
					high ? 'font-medium text-emerald-400' : 'text-foreground',
				)}
			>
				{fmtPctValue(r.median_yield_pct)}
			</td>
			<td className="px-2 py-1.5 text-right text-muted">{fmtPctValue(r.avg_yield_pct)}</td>
			<td className="px-2 py-1.5 text-right text-muted">{fmtPctValue(r.max_yield_pct)}</td>
			<td className="px-2 py-1.5 text-secondary">
				{r.top_symbol ? (
					<span title={`${r.top_symbol} · ${fmtPctValue(r.top_yield_pct)}`}>
						{r.top_name ?? r.top_symbol}{' '}
						<span className="text-muted">{fmtPctValue(r.top_yield_pct)}</span>
					</span>
				) : (
					<span className="text-muted">—</span>
				)}
			</td>
			<td className="px-2 py-1.5 text-right text-muted">
				{r.excluded_count > 0 ? r.excluded_count : '—'}
			</td>
		</tr>
	)
}

/**
 * 降序/升序比较, **null 一律排最后**(与港股分红页同一实现口径)。
 *
 * 🔴 不这么做的话, 点「升序」时所有算不出股息率的行业会排到最前面, 第一屏全是
 * 「—」, 看着像数据全坏了。
 */
function cmpNullLast(a: number | null, b: number | null, dir: 1 | -1): number {
	if (a == null && b == null) return 0
	if (a == null) return 1
	if (b == null) return -1
	return (a - b) * dir
}
