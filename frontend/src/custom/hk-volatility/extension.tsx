import { lazy } from 'react'
import { Activity } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

export const HkVolatility = lazy(() => import('./Page').then(m => ({ default: m.HkVolatility })))

/**
 * 港股波动率情绪 (VHSI) — A 股「情绪周期」在港股的**对应维度**。
 *
 * 走扩展机制而非核心路由: 删掉本目录即整体卸载, 不需要改 router/nav 任何一行。
 *
 * 🔴 刻意**不**做成 A 股 Regime 页上的一个市场开关 (§7 市场差异是数据不是控制流):
 * A 股那套建立在连板梯队上, 而港股无涨跌停 ⇒ 连板结构上不存在, 两者不是同一指标
 * 的两个市场版本, 而是**两个不同维度**。塞进同一页只会逼出一堆 `if (market === …)`。
 *
 * 🔴 也刻意**不**塞进个股详情页: VHSI 是**全市场**单一序列, 不属于任何一只股票。
 */
const extension: FrontendExtension = {
	id: 'official.hk-volatility',
	apiVersion: 1,
	routes: [
		{ id: 'hk-volatility', path: '/hk-volatility', component: HkVolatility },
	],
	navigation: [
		{ id: 'hk-volatility', routeId: 'hk-volatility', label: '港股波动率', icon: Activity, badge: 'HK' },
	],
}

export default extension
