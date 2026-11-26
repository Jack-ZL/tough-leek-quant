import type { ComponentType } from 'react'
import type { ChartPriceLine, ChartRenderer } from '@/lib/chart-types'
import type { KlineMarketSource } from '@/lib/kline-market-adapter'
import type { Market } from '@/lib/market'
import { StockPanel } from '@/components/StockPanel'
import { HkStockDailyPanel } from '@/components/HkStockDailyPanel'

/**
 * 详情弹窗「日K」主体的**市场注册表**。
 *
 * 存在意义只有一个: 让弹窗(`StockPreviewDialogContent`)不必写
 * `market === 'HK' ? <HkStockDailyPanel/> : <StockPanel/>`(方案 §8 规则 2)。
 * 新增市场 = 在 `lib/kline-market-adapter.ts` 的 `KLINE_SOURCES` 加一项 +
 * 在下面的 `BODIES` 加一行, 弹窗与图表零改动。
 *
 * 🔴 这张表**必须**与 `KLINE_SOURCES` 同步: 两者都是 `Record<Market, …>`, 少一个
 * 市场会直接编译失败(TS 会报缺 key), 不会静默漂移成「切到美股渲染 A 股页面」
 * —— 那正是方案 §8 引用的 QuantMind fork 页面事故。
 */

/** 弹窗传给主体的 props。两个市场各取所需, 不支持的直接忽略。 */
export interface StockDetailProps {
	symbol: string
	renderer?: ChartRenderer
	height?: number
	dateRange?: { start: string; end: string }
	/** 详情顶栏已有名称时可隐藏主体内重复的身份信息。 */
	showQuoteIdentity?: boolean
	/** 信息条自定义入口: 宿主偏好加载完成前禁用, 并共用宿主的配置弹窗。 */
	showInfoBarCustomizer?: boolean
	onCustomizeInfoBar?: () => void
	customIndicators?: boolean
	/** 初始可见蜡烛根数 (默认 60); 'all' = 初始适配显示全部数据。 */
	visibleBars?: number | 'all'
	showIntraday?: boolean
	priceLines?: ChartPriceLine[]
	onPriceDoubleClick?: (price: number, currentPrice: number) => void
	onAssetType?: (assetType: 'stock' | 'etf' | 'index') => void
	refetchIntervalMs?: number
	prefetchSymbols?: string[]
	intradayDays?: number
	addedDate?: string | null
}

/** 主体实际收到的 props(注册表补齐 `source` 后下发)。 */
export type StockDetailBodyProps = StockDetailProps & { source: KlineMarketSource }

const BODIES: Record<Market, ComponentType<StockDetailBodyProps>> = {
	// StockPanel 不认 source: A 股是缺省市场, 其查询配置写死在 lib/kline.ts。
	// 包一层把 source 丢掉, 而不是给 StockPanel 加一个它永远不读的 prop。
	CN: ({ source: _source, ...rest }) => <StockPanel {...rest} />,
	HK: HkStockDailyPanel,
}

export function StockDetailBody({
	source,
	...props
}: StockDetailProps & { source: KlineMarketSource | null }) {
	// 🔴 认不出市场就不渲染主体, 也**不兜底 A 股**(请求层不兜底, 见 lib/market.ts
	// 文件头表格)。走到这里说明有调用方绕过了 symbolMarket 那道关, 如实报错比
	// 拿港股代码去打 A 股端点、然后显示一张空图要好发现得多。
	if (source == null) {
		return (
			<div className="text-sm text-danger py-4">
				无法识别的证券代码：{props.symbol}（既不符合 A 股也不符合港股格式）
			</div>
		)
	}
	const Body = BODIES[source.market]
	return <Body source={source} {...props} />
}
