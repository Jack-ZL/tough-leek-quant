import { cn } from '@/lib/cn'

/**
 * 行情延时标注 —— 港股快照是**交易所授权的延时行情**, 属正常状态, 不是故障。
 *
 * 🔴 刻意不复用 `QuoteFreshnessBadge`: 那套的 `state === 'delayed'` 语义是
 * **异常**(`lib/quote-freshness.ts` 文案「请检查数据源与连接」, 且被归进
 * warning/琥珀色一组)。拿它标港股等于告诉用户「你的数据源坏了」—— 而事实是
 * 港股行情本来就延时。本徽章用中性色, 只陈述事实, 不报警。
 *
 * 🔴 分钟数**只能**来自接口字段 `quote_delay_minutes`(后端 `rules_for(market)`
 * 是单一权威, `/api/hk/indices` 与 `/api/hk/index_daily` 都返回它)。组件不备
 * 默认值: 传 `null`/`0`/`undefined` 就不渲染。写死 15 会在源侧调整延时后静默说谎,
 * 也会让 A 股(无此字段)凭空多出延时标注。
 */
export function QuoteDelayBadge({ minutes, className }: {
	/** 接口返回的 `quote_delay_minutes`; 空值或 ≤0 → 不渲染 */
	minutes?: number | null
	className?: string
}) {
	if (!minutes || !Number.isFinite(minutes) || minutes <= 0) return null
	const label = `延时 ${minutes} 分钟`
	const title = `交易所授权的延时行情(约 ${minutes} 分钟), 非数据源故障`
	return (
		<span
			title={title}
			aria-label={label}
			className={cn(
				'inline-flex shrink-0 items-center whitespace-nowrap rounded border border-border/60 px-1 text-[10px] leading-4 text-muted',
				className,
			)}
		>
			{label}
		</span>
	)
}
