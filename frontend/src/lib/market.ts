/**
 * 市场维度共享层 —— 类型与归一化, **不含任何市场专属内容**。
 *
 * 市场差异(标签 / 核心指数 / 货币 / 空态文案)一律住在 `marketContent.ts`,
 * 组件里禁止 `if (market === 'HK')`(方案 §8 规则 2)。这与后端是同一条教义:
 * `backend/tests/test_market_architecture_guard.py` 用 AST 拦下任何
 * `market` 表达式与市场字面量的比较 —— 分支会漏, 表不会。
 *
 * 🔴 兜底方向是**分裂**的, 刻意不统一:
 *
 * | 场景 | 未知 market 怎么办 | 为什么 |
 * |---|---|---|
 * | 展示层(`normalizeMarket`) | 兜底 CN | 宁可显示 A 股文案也不白屏 |
 * | 过滤 / 请求层 | **不兜底**, 当场报错 | 把港股代码当 A 股查会静默返回空, 比报错难发现得多 |
 *
 * 后端同口径: `rules_for(None)` 抛错、`index_const.core_index_names` 对未登记市场
 * 抛 ValueError, 都**不回退 CN**(回退 = 港股页面渲染出上证综指, 属串市场事故)。
 * 所以本文件的兜底只服务渲染, 不要拿它去构造查询参数。
 */

export const MARKETS = ['CN', 'HK'] as const

export type Market = (typeof MARKETS)[number]

/** 展示层缺省市场。仅用于渲染兜底, 见文件头表格。 */
export const DEFAULT_MARKET: Market = 'CN'

/** 是否已登记的市场。大小写敏感 —— 先 `toUpperCase` 再判, 与后端 symbol 归一同理。 */
export function isMarket(value: unknown): value is Market {
	return typeof value === 'string' && (MARKETS as readonly string[]).includes(value)
}

/**
 * 展示层归一化: 容忍大小写与首尾空白, 未知 → `DEFAULT_MARKET`(不白屏)。
 *
 * ⚠️ **不要**用它归一化查询参数或过滤条件(见文件头表格)。
 */
export function normalizeMarket(value: unknown): Market {
	if (typeof value !== 'string') return DEFAULT_MARKET
	const upper = value.trim().toUpperCase()
	return isMarket(upper) ? upper : DEFAULT_MARKET
}
