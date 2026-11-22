import {lazy} from 'react'
import {Flame} from 'lucide-react'
import type {FrontendExtension} from '@/extensions/types'

export const HkHotRank = lazy(() => import('./Page').then(m => ({default: m.HkHotRank})))

/**
 * 港股人气榜 — 东方财富股吧个股人气榜(前 100 名)。
 *
 * 走扩展机制而非核心路由: 删掉本目录即整体卸载, 不需要改 router/nav 任何一行。
 *
 * 🔴 这是 A 股「人气/热度」在港股的**对应维度**, 不是同一榜单的港股版:
 * A 股侧的热度来自同花顺并经 `ext_data` 落到个股行上(见 `api.ts` 的 `hot_rank`),
 * 港股侧是东方财富股吧的独立排名体系, 两者数据源、口径、覆盖范围都不同,
 * 不可互相比较或混排。
 *
 * 🔴 也刻意**不**塞进个股详情页: 榜单是**全市场**排名, 单只股票的名次只有在
 * 「第 N / 共 M」的语境里才可解读(详见 Page 里的分母说明)。
 */
const extension: FrontendExtension = {
	id: 'official.hk-hot-rank',
	apiVersion: 1,
	routes: [
		{id: 'hk-hot-rank', path: '/hk-hot-rank', component: HkHotRank},
	],
	navigation: [
		{id: 'hk-hot-rank', routeId: 'hk-hot-rank', label: '港股人气榜', icon: Flame},
	],
}

export default extension
