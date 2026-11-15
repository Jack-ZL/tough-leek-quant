import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { StockDailyKChart, getDefaultRange } from '@/components/StockDailyKChart'
import { QuoteDelayBadge } from '@/components/QuoteDelayBadge'
import type { HkDailyResponse } from '@/lib/api'
import type { KlineMarketSource } from '@/lib/kline-market-adapter'
import type { ChartRenderer } from '@/lib/chart-types'

/**
 * 港股个股详情「日K」主体。
 *
 * 与 A 股 `StockPanel` **并列**的市场实现, 不是它内部的分支(方案 §8 规则 2)。
 * 港股没有实时行情链路(方案 §3.4), 所以这里没有报价条 / SSE / 分时 / 财务 /
 * 最新一根增量 / 涨跌停标记 —— 按方案 §8 规则 4「未就绪直接隐藏入口」, 而不是
 * 渲染一堆必然为空的 A 股区块, 让用户以为数据坏了。
 *
 * 图表本体仍然共用 `StockDailyKChart`: 两个市场的日K行字段都是 `KlineRow`, 差异
 * (端点 / query key / 空态文案)全部由 `lib/kline-market-adapter.ts` 经 `source`
 * 注入。**本文件不出现任何 `if (market === …)`**, 也不写死延时分钟数与币种 ——
 * 前者来自响应的 `quote_delay_minutes`, 后者来自 `currency`。
 *
 * 🔴 `source` 走 props 而不是内部 `klineSource('HK')`: 后者会与
 * `StockDetailBody.tsx` 形成「适配器 ↔ 组件」的循环 import(注册表要引用本组件)。
 */

/**
 * 成交量纲的中文标签。
 *
 * 🔴 港股是**股**, A 股行情列表惯用**手**(1 手 = 100 股)。标错会让读者把成交量
 * 读成 100 倍 —— 后端 `shares_per_volume_unit` 对两个市场分别返回 1.0 / 100.0,
 * 换手率公式里就差这一个系数。未知值原样透出, 不猜。
 */
const VOLUME_UNIT_LABEL: Record<string, string> = { shares: '股', lots: '手' }

export interface HkStockDailyPanelProps {
	symbol: string
	source: KlineMarketSource
	renderer?: ChartRenderer
	height?: number
	dateRange?: { start: string; end: string }
	/** 加入自选日。港股暂无自选入口(后端 watchlist 零市场感知), 恒为 null。 */
	addedDate?: string | null
}

export function HkStockDailyPanel({
	symbol,
	source,
	renderer = 'lightweight',
	height = 420,
	dateRange,
	addedDate,
}: HkStockDailyPanelProps) {
	const range = dateRange ?? getDefaultRange()
	// useMemo: options 对象每渲染都换身份会让 useQuery 反复重建 observer。
	// 依赖取 start/end 而不是 range 对象本身 —— 后者在 dateRange 缺省时每次都是新的。
	const queryOptions = useMemo(
		() => source.dailyQueryOptions(symbol, { start: range.start, end: range.end }),
		[source, symbol, range.start, range.end],
	)
	// 与 StockDailyKChart 内部那次 useQuery 同 key ⇒ React Query 去重, 只发一次请求。
	// 这里单独取一份是为了读 empty_reason / quote_delay_minutes / currency:
	// 图表组件不认这些字段(它只消费最小面 KlineChartPayload)。
	const { data } = useQuery({ ...queryOptions, enabled: !!symbol })
	const hk = data as HkDailyResponse | undefined
	const volumeUnit = hk?.volume_unit ? (VOLUME_UNIT_LABEL[hk.volume_unit] ?? hk.volume_unit) : null

	return (
		<div className="flex min-w-0 flex-col gap-2">
			<div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
				<span className="font-mono text-sm font-semibold text-foreground">{symbol}</span>
				{/* 维表未同步时 name 为 null(后端如实报告), 此时只显示代码 */}
				{hk?.name && <span className="text-secondary">{hk.name}</span>}
				<span className="rounded border border-border/60 bg-elevated px-1.5 py-0.5 text-[10px] text-secondary">
					{source.content.label}
				</span>
				{/* 🔴 分钟数只能来自接口字段; null/0 时组件自行不渲染(见 QuoteDelayBadge) */}
				<QuoteDelayBadge minutes={hk?.quote_delay_minutes} />
				{hk?.currency && (
					<span
						className="text-[10px] text-muted"
						title="计价币种与成交量纲取自接口字段，前端不写死。港股成交量以「股」计，与 A 股行情列表的「手」相差 100 倍。"
					>
						{hk.currency}{volumeUnit ? ` · 量纲 ${volumeUnit}` : ''}
					</span>
				)}
			</div>
			<StockDailyKChart
				symbol={symbol}
				renderer={renderer}
				height={height}
				dateRange={range}
				queryOptions={queryOptions}
				emptyHint={source.dailyEmptyHint(hk?.empty_reason)}
				// 港股无涨跌停(后端 limit_* 列恒为 null)。显式关掉而不是依赖「算不出标记」:
				// 万一将来 buildLimitMarkers 改成按百分比推算, 那对港股就是编造。
				showLimitMarkers={false}
				addedDate={addedDate}
			/>
		</div>
	)
}
