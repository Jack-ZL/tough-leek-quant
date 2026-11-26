import { lazy } from 'react'
import { HandCoins } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

export const HkDividends = lazy(() => import('./Page').then(m => ({ default: m.HkDividends })))

/**
 * 港股分红日历 (PR7 · 港股独有维度之一)。
 *
 * 走扩展机制而非核心路由: 删掉本目录即整体卸载, 不需要改 router/nav 任何一行。
 * 🔴 刻意**不**做成「A 股页面上加 market 开关」: 分红日历只有港股数据源, A 股侧
 * 没有对应报表, 同页切市场只会得到一个永远为空的 A 股 tab (§7 不用前端补丁掩盖
 * 后端能力差异)。
 */
const extension: FrontendExtension = {
	id: 'official.hk-dividends',
	apiVersion: 1,
	routes: [
		{ id: 'hk-dividends', path: '/hk-dividends', component: HkDividends },
	],
	navigation: [
		{ id: 'hk-dividends', routeId: 'hk-dividends', label: '港股分红', icon: HandCoins, badge: 'HK' },
	],
}

export default extension
