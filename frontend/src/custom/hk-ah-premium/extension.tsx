import {lazy} from 'react'
import {Scale} from 'lucide-react'
import type {FrontendExtension} from '@/extensions/types'

export const HkAhPremium = lazy(() => import('./Page').then(m => ({default: m.HkAhPremium})))

/**
 * AH 溢价 — A 股与 H 股同股价差, 跨市场维度。
 *
 * 走扩展机制而非核心路由: 删掉本目录即整体卸载, 不需要改 router/nav 任何一行。
 * 🔴 刻意**不**塞进 A 股个股详情页当一栏: 溢价是「一对标的之间的关系」, 不是单只
 * 股票的属性; 而且它是本仓库唯一被允许做汇率换算的指标, 汇率来源与日期必须与
 * 结果同屏可见, 塞进详情页会把这条约束拆散 (§7 不用前端补丁掩盖后端能力差异)。
 */
const extension: FrontendExtension = {
	id: 'official.hk-ah-premium',
	apiVersion: 1,
	routes: [
		{id: 'hk-ah-premium', path: '/hk-ah-premium', component: HkAhPremium},
	],
	navigation: [
		{id: 'hk-ah-premium', routeId: 'hk-ah-premium', label: 'AH 溢价', icon: Scale},
	],
}

export default extension
