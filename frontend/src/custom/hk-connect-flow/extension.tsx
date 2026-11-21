import {lazy} from 'react'
import {Wallet} from 'lucide-react'
import type {FrontendExtension} from '@/extensions/types'

export const HkConnectFlow = lazy(() => import('./Page').then(m => ({default: m.HkConnectFlow})))

/**
 * 港股通资金流(南向) — 港股独有维度。
 *
 * 走扩展机制而非核心路由: 删掉本目录即整体卸载, 不需要改 router/nav 任何一行。
 * 🔴 刻意**不**做成「A 股资金流页面上加 market 开关」: 南向资金是港股专属口径
 * (内地资金买港股), A 股侧的对应物是北向, 两者报表、通道划分、币种都不同,
 * 同页切市场只会得到一堆永远为空的列 (§7 不用前端补丁掩盖后端能力差异)。
 */
const extension: FrontendExtension = {
	id: 'official.hk-connect-flow',
	apiVersion: 1,
	routes: [
		{id: 'hk-connect-flow', path: '/hk-connect-flow', component: HkConnectFlow},
	],
	navigation: [
		{id: 'hk-connect-flow', routeId: 'hk-connect-flow', label: '南向资金', icon: Wallet},
	],
}

export default extension
