// 全局市场状态 — 侧栏市场切换器的唯一状态源
//
// 🔴 **作用范围是收窄的, 不是「整个 App 切市场」**。当前只有三类面对港股就绪:
//
//   1. 侧栏指数条 + 指数页 (`lib/index-market-adapter.ts` 的 `indexSource`)
//   2. 个股详情抽屉 —— 但它由**代码后缀**判市场 (`symbolMarket`), 不读本 store
//   3. 财务概况页 (`lib/financial-market-adapter.ts` 的 `financialSource`) —— 读本 store;
//      港股走 `/api/hk/financials*` 独立端点、独立字段表与独立 query key 前缀
//      (`hk-financials`), 切市场时清空已选标的 (A 股代码打港股端点会 400)
//
// 核心导航 8 项里其余 7 项 (看板/自选/策略/因子/回测/市场环境/数据) 只有 A 股链路:
// 自选读 `repository.get_enriched_latest()` (零 market 参数, 只扫 `kline_enriched/`),
// 看板/策略/因子/回测同理。让它们跟随本 store 切到 HK 只有两种结局 —— 继续显示
// A 股数据 (§15 登记过的串市场事故, QuantMind 出过), 或整页空白。所以**导航条目
// 不按市场过滤**, 切换器 UI 上明写作用范围 (`MARKET_SWITCHER_SCOPE_HINT`)。
//
// 自选接通 (方案 PR3 剩余的 `get_enriched_latest(market=...)`) 之后再逐项扩大消费面;
// 扩大一项就在上面清单里加一项, 不要先改 store 语义再补页面。
//
// 机制: 模块级变量 + Set<listener> + useSyncExternalStore (与 stockPreviewStore /
// aiReportStore 同构, 项目无状态库)。localStorage 持久化 + storage 事件跨标签页同步
// (与 lib/theme.ts 同构)。
import { useSyncExternalStore } from 'react'
import { DEFAULT_MARKET, isMarket, type Market } from './market'

const KEY = 'tf-market'

// 🔴 读取路径用 isMarket 严格校验而不是 normalizeMarket: 这里存的是**用户上次的
// 明确选择**, 落库值非法 (手改 localStorage / 旧版本残留) 时应当回落默认市场,
// 而不是把任意字符串「规整」成一个看似合法的市场再拿它去拼请求参数
// (lib/market.ts 的分流铁律: 展示层可兜底, 请求层不可)。
function readStored(): Market {
	try {
		const v = localStorage.getItem(KEY)
		return isMarket(v) ? v : DEFAULT_MARKET
	} catch {
		return DEFAULT_MARKET
	}
}

let market: Market = readStored()
const listeners = new Set<() => void>()

function emit() {
	listeners.forEach(l => l())
}

export function getMarket(): Market {
	return market
}

export function subscribeMarket(listener: () => void): () => void {
	listeners.add(listener)
	return () => { listeners.delete(listener) }
}

/** 切换全局市场。写 localStorage 即时缓存并广播 (本页 + 跨标签页)。 */
export function setMarket(next: Market) {
	if (!isMarket(next) || next === market) return
	market = next
	try {
		localStorage.setItem(KEY, next)
	} catch { /* 隐私模式下写不进, 内存态仍然生效 */ }
	emit()
}

// 跨标签页同步: 另一个标签页切了市场, 本标签页的指数条/指数页要跟着换,
// 否则两个标签页会各自请求不同市场却共用同一个 queryKey 前缀。
if (typeof window !== 'undefined') {
	window.addEventListener('storage', (e) => {
		if (e.key !== KEY && e.key !== null) return
		const next = readStored()
		if (next === market) return
		market = next
		emit()
	})
}

/** hook: 当前全局市场 (切换自动触发重渲染)。 */
export function useMarket(): Market {
	return useSyncExternalStore(subscribeMarket, getMarket, getMarket)
}

/**
 * 切换器上必须展示的作用范围说明。
 *
 * 🔴 不要删也不要改成「切换市场」这类无范围措辞 —— 用户看到全局位置的切换器会
 * 默认它切整个 App, 而 7/8 的核心页面并不跟随。这行文案是「不误导」的唯一防线
 * (CONTRIBUTING §4 fail closed: 宁可明说没做, 不可让人以为做了)。
 */
export const MARKET_SWITCHER_SCOPE_HINT = '当前仅指数、个股详情与财务概况支持港股，其余页面仍为 A 股'

/** A 股(缺省市场)的作用范围说明: 全链路皆 A 股, 无需「仍为 A 股」的对比措辞。 */
export const MARKET_SWITCHER_SCOPE_HINT_CN = '当前所有页面均为 A 股数据，来源公开的 API 或平台，不支持港股数据'

/** 切换器提示文案: 按市场查表, 组件不写市场分支。 */
export function marketSwitcherScopeHint(market: Market): string {
	return market === DEFAULT_MARKET ? MARKET_SWITCHER_SCOPE_HINT_CN : MARKET_SWITCHER_SCOPE_HINT
}
