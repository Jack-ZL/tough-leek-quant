/**
 * 查询缓存持久化 — 解决「每次打开应用都全量重新拉数据、等很久」。
 *
 * 仅白名单内的慢变查询族落地 localStorage: 重开应用时先渲染上一次的数据
 * (SWR: 挂载后仍按各自 staleTime 后台刷新), 秒开有内容。快变行情类
 * (watchlist-enriched 实时价 / 分钟K / 告警 / overview-market 等) 不持久化,
 * 避免陈旧价格误导。白名单外的查询仍是纯内存缓存, 行为与原先完全一致。
 *
 * 键首段精确匹配 (不是前缀匹配): 'watchlist' 不会误命中 'watchlist-enriched'。
 */
import {createAsyncStoragePersister} from '@tanstack/query-async-storage-persister'

/** 持久化结构版本: 序列化结构变化时递增, 旧缓存整体作废 */
export const PERSIST_BUSTER = 'v1'

/** 允许持久化的查询键首段 (对齐 lib/queryKeys.ts 各工厂首段与个别内联键) */
const PERSISTABLE_KEY_ROOTS = new Set([
	'settings',                 // 应用设置 (OnboardingGuard 依赖; 恢复后打开不再整屏 logo)
	'preferences',              // 用户偏好
	'capability-matrix',        // 能力路由矩阵 (慢变)
	'watchlist',                // 自选清单 (不含 enriched/quotes 等行情族)
	'watchlist-groups',
	'screener-strategies',      // 策略池清单
	'regime-history',           // 以下 regime 族一天最多变一次
	'regime-states',
	'regime-coverage',
	'regime-phases',
	'regime-mainline',
	'custom-signals',           // 自定义信号库
	'custom-signals-options',
])

function isPersistableQueryKey(key: readonly unknown[]): boolean {
	return typeof key[0] === 'string' && PERSISTABLE_KEY_ROOTS.has(key[0])
}

/** dehydrate 过滤: 只落地白名单族的成功查询 (默认规则已排除 pending/error) */
export function shouldPersistQuery(query: { queryKey: readonly unknown[]; state: { status: string } }): boolean {
	return query.state.status === 'success' && isPersistableQueryKey(query.queryKey)
}

/** localStorage persister (throttle 2s 合并高频写)。
 * 用官方推荐的 createAsyncStoragePersister (sync 版已弃用 TS6385):
 * localStorage 本身同步, 包一层 Promise 适配 AsyncStorage 接口即可,
 * 存储键与序列化格式不变, 旧缓存无缝沿用。 */
export function createAppPersister() {
	return createAsyncStoragePersister({
		storage: {
			getItem: async key => window.localStorage.getItem(key),
			setItem: async (key, value) => window.localStorage.setItem(key, value),
			removeItem: async key => window.localStorage.removeItem(key),
		},
		key: 'tf-query-cache',
		throttleTime: 2_000,
	})
}
