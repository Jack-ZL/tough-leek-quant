// @vitest-environment jsdom
// 个股分析看板的市场路由回归闸 (CONTRIBUTING §3.1 / §4 / §7):
//   1. 🔴 港股代码 (5 位 + .HK) **必须**走 /api/hk/daily (api.hkDaily), A 股走
//      /api/kline/daily (api.klineDaily)。/api/kline/daily 只读 A 股 enriched,
//      港股代码会静默返回空 rows ⇒ 误报「暂无日K」—— 把「数据在另一张表」伪装成
//      「没有数据」, 是静默给出误导性结论级别的缺陷, 肉眼 review 看不出来。
//   2. 🔴 判据用 symbolMarket (按显式后缀, 不兜底 CN), 不得按位数/前缀臆测市场。
//   3. 🔴 港股空态必须按 empty_reason 归因: no_enriched → 引导「重算 enriched」,
//      no_daily → 引导「同步港股日K」。一律「去同步」会把指标未算出误导成没数据。
//   4. 🔴 A 股路径与空态文案保持原样, 不得被港股分支污染。
//   5. 🔴 两市场行类型同为 KlineRow[], 图表直接复用 —— 港股 rows 必须原样进图表。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { HkDailyResponse, KlineDailyResponse } from '@/lib/api'

const h = vi.hoisted(() => ({
	klineDaily: vi.fn(),
	hkDaily: vi.fn(),
	levels: vi.fn(),
	chartRows: null as null | unknown[],
}))

vi.mock('@/lib/api', async importActual => {
	const actual = await importActual<typeof import('@/lib/api')>()
	return {
		...actual,
		api: {
			...actual.api,
			klineDaily: (...a: unknown[]) => h.klineDaily(...a),
			hkDaily: (...a: unknown[]) => h.hkDaily(...a),
			stockAnalysisLevels: (...a: unknown[]) => h.levels(...a),
		},
	}
})

// 图表层换桩: 只记录传入的 rows, 避免 jsdom 无 canvas。路由对不对看 rows 是否到达。
vi.mock('@/components/stock-analysis/AnalysisKChart', () => ({
	AnalysisKChart: (props: { rows?: unknown[] }) => {
		h.chartRows = props.rows ?? null
		return <div data-testid="kchart" data-rows={String(props.rows?.length ?? 0)} />
	},
	LEVEL_GROUPS: {},
}))

import { StockAnalysisBoard } from './Page'

const ROWS = [
	{ date: '2026-11-18', open: 10, high: 11, low: 9, close: 10.5, volume: 1000 },
	{ date: '2026-11-19', open: 10.5, high: 11.5, low: 10, close: 11, volume: 1100 },
	{ date: '2026-11-20', open: 11, high: 12, low: 10.5, close: 11.5, volume: 1200 },
]

function hkPayload(rows: unknown[], emptyReason: HkDailyResponse['empty_reason'] = null): HkDailyResponse {
	return {
		symbol: '00700.HK', name: '腾讯控股', market: 'HK', volume_unit: 'shares',
		currency: 'HKD', quote_delay_minutes: 15, empty_reason: emptyReason, rows,
	} as HkDailyResponse
}

function cnPayload(rows: unknown[]): KlineDailyResponse {
	return { symbol: '600519.SH', rows } as KlineDailyResponse
}

let host: HTMLDivElement | null = null
let root: Root | null = null

beforeEach(() => {
	Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
	h.klineDaily.mockReset()
	h.hkDaily.mockReset()
	h.levels.mockReset()
	h.chartRows = null
	// 关键价位查询与路由无关, 给个最小可解析值, 避免未配置桩抛错。
	h.levels.mockResolvedValue({ levels: {}, close: 11.5 })
	host = document.createElement('div')
	document.body.appendChild(host)
})

afterEach(() => {
	root?.unmount()
	root = null
	host?.remove()
	host = null
})

async function mount(symbol: string): Promise<void> {
	const client = new QueryClient({
		defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } },
	})
	root = createRoot(host!)
	await act(async () => {
		root!.render(
			<QueryClientProvider client={client}>
				<StockAnalysisBoard symbol={symbol} />
			</QueryClientProvider>,
		)
	})
}

/** react-query 通知批处理需要多轮微任务, 单次 act flush 不够 — 轮询等待断言成立。 */
async function settled(check: () => void): Promise<void> {
	await vi.waitFor(async () => {
		await act(async () => { await new Promise(r => setTimeout(r, 0)) })
		check()
	}, { timeout: 3000 })
}

function text(): string { return host?.textContent ?? '' }
function kchart(): HTMLElement | null { return host!.querySelector('[data-testid="kchart"]') }

it('港股代码走 hkDaily 而非 klineDaily, rows 原样进图表', async () => {
	h.hkDaily.mockResolvedValue(hkPayload(ROWS))
	h.klineDaily.mockResolvedValue(cnPayload([]))
	await mount('00700.HK')
	await settled(() => expect(kchart()).not.toBeNull())
	expect(h.hkDaily).toHaveBeenCalledWith('00700.HK', { limit: 250 })
	expect(h.klineDaily).not.toHaveBeenCalled()
	expect(kchart()!.getAttribute('data-rows')).toBe('3')
	expect(h.chartRows).toHaveLength(3)
})

it('A 股代码走 klineDaily 而非 hkDaily, 路径不被港股分支污染', async () => {
	h.klineDaily.mockResolvedValue(cnPayload(ROWS))
	h.hkDaily.mockResolvedValue(hkPayload([]))
	await mount('600519.SH')
	await settled(() => expect(kchart()).not.toBeNull())
	expect(h.klineDaily).toHaveBeenCalledWith('600519.SH', 250)
	expect(h.hkDaily).not.toHaveBeenCalled()
	expect(kchart()!.getAttribute('data-rows')).toBe('3')
})

it('港股 empty_reason=no_enriched 引导重算 enriched, 不渲染空图表', async () => {
	h.hkDaily.mockResolvedValue(hkPayload([], 'no_enriched'))
	await mount('00700.HK')
	await settled(() => expect(text()).toMatch(/重算 enriched/))
	expect(kchart()).toBeNull()
	expect(text()).not.toMatch(/该港股尚未同步日 K/)
})

it('港股 empty_reason=no_daily 引导同步港股日K', async () => {
	h.hkDaily.mockResolvedValue(hkPayload([], 'no_daily'))
	await mount('00700.HK')
	await settled(() => expect(text()).toMatch(/该港股尚未同步日 K/))
	expect(kchart()).toBeNull()
	expect(text()).not.toMatch(/重算 enriched/)
})

it('A 股空态文案保持原样, 不套用港股归因', async () => {
	h.klineDaily.mockResolvedValue(cnPayload([]))
	await mount('600519.SH')
	await settled(() => expect(text()).toMatch(/该标的尚未同步日 K/))
	expect(kchart()).toBeNull()
	expect(text()).not.toMatch(/港股/)
})
