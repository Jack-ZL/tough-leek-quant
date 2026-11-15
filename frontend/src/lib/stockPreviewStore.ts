import type {OpenStockPreviewOptions, StockPreviewItem, StockPreviewUi} from '@/extensions/stockPreview'
import {symbolMarket} from '@/lib/marketContent'
import {uniqueBy, navItemKey, type NavItem} from './listNav'

interface StockPreviewRequest {
	symbol: string
	name?: string
	navList?: NavItem[]
}

let request: StockPreviewRequest | null = null
const listeners = new Set<() => void>()

function normalizeItem(value: unknown): NavItem | null {
	if (!value || typeof value !== 'object') return null
	const item = value as Partial<StockPreviewItem>
	if (typeof item.symbol !== 'string') return null
	const symbol = item.symbol.trim().toUpperCase()
	// 🔴 市场判定只认**显式交易所后缀**(`symbolMarket` 遍历 marketContent 的表),
	// 认不出就拒 —— 过滤层不兜底(见 `lib/market.ts` 文件头: 把港股代码当 A 股查
	// 会静默返回空, 比报错难发现得多)。旧实现写死 A 股正则, 港股代码进不了详情
	// 弹窗, 而弹窗是 K线/详情的唯一入口。
	if (symbolMarket(symbol) === null) return null
	return {symbol, name: typeof item.name === 'string' ? item.name.trim() || undefined : undefined}
}

function open(options: OpenStockPreviewOptions): boolean {
	const item = normalizeItem(options)
	if (!item || (options.navList != null && !Array.isArray(options.navList))) return false
	const navList = options.navList ? uniqueBy(options.navList.flatMap(value => {
		const normalized = normalizeItem(value)
		return normalized ? [normalized] : []
	}), navItemKey) : undefined
	request = {...item, name: item.name ?? navList?.find(row => row.symbol === item.symbol)?.name, navList}
	listeners.forEach(listener => listener())
	return true
}

function close(): void {
	if (!request) return
	request = null
	listeners.forEach(listener => listener())
}

/** 仅由宿主安装到能力桥，不随源码扩展的独立编译产物复制状态。 */
export const stockPreviewUi: StockPreviewUi = {openStockPreview: open, closeStockPreview: close}

export function getStockPreviewRequest() {
	return request
}

export function subscribeStockPreview(listener: () => void) {
	listeners.add(listener)
	return () => {
		listeners.delete(listener)
	}
}

export function navigateStockPreview(symbol: string, name?: string): void {
	const item = normalizeItem({symbol, name})
	if (!request || !item) return
	request = {...request, ...item}
	listeners.forEach(listener => listener())
}
