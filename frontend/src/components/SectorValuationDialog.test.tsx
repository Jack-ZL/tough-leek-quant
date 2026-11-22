// @vitest-environment jsdom
/**
 * 行业估值温度计对话框 (PR6)。
 *
 * 钉三件最容易被「顺手改一下」弄坏的事:
 *
 * 1. 🔴 **PE 缺源必须明示, 且绝不凭空造一列 PE**。后端下发 `pe_available: false`
 *    + 理由, 前端把理由原样显示。留一个空 PE 列会被读成「这个行业 PE 为空」,
 *    而真相是「我们没有源」—— 两种说法在 §4 下完全不同。
 * 2. 🔴 **`null` 与 `0` 是两种状态**: `null` 渲染「—」(算不出), `0.0` 渲染
 *    `0.00%`(查证过、确无派息)。同一个行业里两列分别落在这两种状态上, 是最容易
 *    被 `?? 0` 一把抹平的地方。
 * 3. 🔴 **默认排序 = 中位数降序, null 排最后**。点升序时 null 仍排最后 —— 否则
 *    第一屏全是「—」, 看着像数据全坏了。
 *
 * 另外钉: 均值标注「参考」(受仙股扭曲)、`excluded_count` 逐行可见、全市场计数
 * 可审计、上游 502 显示错误而不是空表。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SectorValuationDialog } from './SectorValuationDialog'
import { api, type HkSectorValuationResponse } from '@/lib/api'

vi.mock('@/lib/api', () => ({ api: { hkSectorValuation: vi.fn() } }))

const PE_REASON =
	'港股全市场 PE 无可用源: 东财 clist 的市盈率字段走 push2 集群(本机 IP 级封禁), ' +
	'财务宽表的 pe_ttm 是同步时点快照且不带 as-of 日期, 无法判断新旧。'

/**
 * 三行覆盖三种状态:
 * - 银行: 高覆盖 + 高中位数 (正常行)
 * - 半导体: 低覆盖 + 低中位数 (方案点名的「价值洼地识别」反例)
 * - 地产建造: **零派息** —— `coverage_pct` 是查证过的 `0.0`, 而三个股息率统计量
 *   是 `null`(无人派息 ⇒ 中位数算不出)。这一行专门用来钉 null≠0。
 */
function resp(overrides: Partial<HkSectorValuationResponse> = {}): HkSectorValuationResponse {
	return {
		items: [
			{
				industry: '银行', member_count: 45, priced_count: 44, payer_count: 39,
				excluded_count: 1, coverage_pct: 88.64, median_yield_pct: 5.61,
				avg_yield_pct: 5.9, max_yield_pct: 9.2,
				top_symbol: '00005.HK', top_name: '汇丰控股', top_yield_pct: 9.2,
			},
			{
				industry: '半导体', member_count: 24, priced_count: 20, payer_count: 5,
				excluded_count: 4, coverage_pct: 25.0, median_yield_pct: 0.93,
				avg_yield_pct: 3.1, max_yield_pct: 8.0,
				top_symbol: '00981.HK', top_name: '中芯国际', top_yield_pct: 8.0,
			},
			{
				industry: '地产建造', member_count: 60, priced_count: 55, payer_count: 0,
				excluded_count: 5, coverage_pct: 0.0, median_yield_pct: null,
				avg_yield_pct: null, max_yield_pct: null,
				top_symbol: null, top_name: null, top_yield_pct: null,
			},
		],
		total: 3,
		market: 'HK',
		currency: 'HKD',
		source: 'eastmoney',
		days: 365,
		window_start: '2025-11-23',
		window_end: '2026-11-22',
		event_count: 1625,
		member_total: 129,
		priced_total: 119,
		payer_total: 44,
		excluded_total: 10,
		unclassified_count: 7,
		pe_available: false,
		pe_unavailable_reason: PE_REASON,
		...overrides,
	}
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
	Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
	vi.clearAllMocks()
	vi.mocked(api.hkSectorValuation).mockResolvedValue(resp())
	host = document.createElement('div')
	document.body.append(host)
	root = createRoot(host)
	client = new QueryClient({
		defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } },
	})
})

afterEach(async () => {
	await act(async () => root.unmount())
	client.clear()
	host.remove()
})

/** 🔴 必须是**宏任务**: react-query 的 settle 走微任务队列, 只 await Promise 抓不到。 */
async function settled() {
	for (let i = 0; i < 5; i++) {
		await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
	}
}

async function mount() {
	await act(async () => root.render(
		<QueryClientProvider client={client}>
			<SectorValuationDialog onClose={() => {}} market="HK" />
		</QueryClientProvider>,
	))
	await settled()
}

const text = () => host.textContent ?? ''

/** 表体每行的文本, 用来断言**顺序**(textContent 拼不出行边界)。 */
function bodyRows(): string[] {
	return [...(host.querySelector('[data-testid="sv-body"]')?.children ?? [])]
		.map(tr => tr.textContent ?? '')
}

describe('PE 缺源', () => {
	it('🔴 显示后端下发的缺源理由, 而不是自己编一份', async () => {
		await mount()
		const banner = host.querySelector('[data-testid="sv-pe-unavailable"]')
		expect(banner).not.toBeNull()
		expect(banner!.textContent).toContain('无可用源')
		expect(banner!.textContent).toContain('pe_ttm')
		expect(text()).toContain('本表不含 PE')
	})

	it('🔴 绝不凭空造一列 PE (空列会被读成「这个行业 PE 为空」)', async () => {
		await mount()
		const headers = [...host.querySelectorAll('thead th')].map(th => th.textContent ?? '')
		expect(headers.some(h => /PE|市盈率/i.test(h))).toBe(false)
		// 表体里也不该出现任何 PE 数值
		expect(bodyRows().join('')).not.toMatch(/PE|市盈率/)
	})
})

describe('null 与 0 是两种状态', () => {
	it('🔴 零派息行业: 覆盖率是查证过的 0.0%, 三个股息率统计量是「—」', async () => {
		await mount()
		const row = bodyRows().find(r => r.includes('地产建造'))
		expect(row).toBeDefined()
		// 覆盖率 0.0 → 渲染成 0.0%, 不是「—」
		expect(row).toContain('0.0%')
		// 中位/均值/最高都是 null → 渲染成「—」, 不能被 `?? 0` 抹成 0.00%
		expect(row).toContain('—')
		expect(row).not.toContain('0.00%')
	})

	it('🔴 有派息的行业渲染真实百分比数值, 且**不再乘 100**', async () => {
		await mount()
		const row = bodyRows().find(r => r.includes('银行'))
		expect(row).toBeDefined()
		expect(row).toContain('5.61%')
		// 若误用 lib/format.ts 的 fmtPct, 5.61 会变成 561.00%
		expect(row).not.toContain('561')
	})

	it('龙头派息者为 null 时显示「—」, 不显示空字符串', async () => {
		await mount()
		const row = bodyRows().find(r => r.includes('地产建造'))
		expect(row).toContain('—')
		expect(row).not.toContain('null')
	})
})

describe('排序', () => {
	it('🔴 默认按中位数降序, 算不出的行业沉到最后', async () => {
		await mount()
		const order = bodyRows().map(r =>
			r.includes('银行') ? '银行' : r.includes('半导体') ? '半导体' : '地产建造')
		expect(order).toEqual(['银行', '半导体', '地产建造'])
	})

	it('🔴 点升序后 null 仍排最后 (否则第一屏全是「—」, 像数据坏了)', async () => {
		await mount()
		const btn = [...host.querySelectorAll('thead button')]
			.find(b => (b.textContent ?? '').includes('中位股息率'))
		expect(btn).toBeDefined()
		await act(async () => btn!.dispatchEvent(new MouseEvent('click', { bubbles: true })))
		await settled()
		const order = bodyRows().map(r =>
			r.includes('银行') ? '银行' : r.includes('半导体') ? '半导体' : '地产建造')
		expect(order).toEqual(['半导体', '银行', '地产建造'])
	})
})

describe('口径可见性', () => {
	it('均值列标注「参考」(受仙股一次性派息扭曲, 不得当头条数字)', async () => {
		await mount()
		const headers = [...host.querySelectorAll('thead th')].map(th => th.textContent ?? '')
		expect(headers.some(h => h.includes('均值') && h.includes('参考'))).toBe(true)
	})

	it('🔴 全市场计数可见 ⇒「是不是全市场」可被肉眼审计', async () => {
		await mount()
		const meta = host.querySelector('[data-testid="sv-meta"]')!.textContent!
		expect(meta).toContain('129')   // member_total
		expect(meta).toContain('119')   // priced_total
		expect(meta).toContain('44')    // payer_total
		expect(meta).toContain('1625')  // event_count
		expect(meta).toContain('未分类 7')
	})

	it('🔴 「算不出」的成分数逐行可见 (它们不进统计, 也绝不当成 0%)', async () => {
		await mount()
		const row = bodyRows().find(r => r.includes('半导体'))
		expect(row).toContain('4')  // excluded_count
		const headers = [...host.querySelectorAll('thead th')].map(th => th.textContent ?? '')
		expect(headers.some(h => h.includes('算不出'))).toBe(true)
	})

	it('页脚写明「中位数只在派息者上算」与「不复权收盘价」', async () => {
		await mount()
		expect(text()).toContain('派息者')
		expect(text()).toContain('不复权')
		expect(text()).toContain('除净日')
	})

	it('窗口与行业数写在标题栏', async () => {
		await mount()
		const win = host.querySelector('[data-testid="sv-window"]')!.textContent!
		expect(win).toContain('2025-11-23')
		expect(win).toContain('2026-11-22')
		expect(win).toContain('365')
		expect(win).toContain('3 个行业')
	})
})

describe('请求与失败', () => {
	it('🔴 按 TTM 窗口请求, 不落库 ⇒ 必须带 staleTime', async () => {
		await mount()
		expect(api.hkSectorValuation).toHaveBeenCalledTimes(1)
		expect(vi.mocked(api.hkSectorValuation).mock.calls[0][0]).toMatchObject({ days: 365 })
	})

	it('🔴 上游 502 显示错误, 不降级成空表 (「拉不到」≠「没人派息」)', async () => {
		vi.mocked(api.hkSectorValuation).mockRejectedValue(new Error('港股分红数据源不可用'))
		await mount()
		expect(host.querySelector('[data-testid="sv-error"]')).not.toBeNull()
		expect(text()).toContain('数据源不可用')
		expect(host.querySelector('[data-testid="sv-empty"]')).toBeNull()
	})

	it('空表时指路去同步, 不是干巴巴的「暂无数据」', async () => {
		vi.mocked(api.hkSectorValuation).mockResolvedValue(resp({ items: [], total: 0 }))
		await mount()
		expect(host.querySelector('[data-testid="sv-empty"]')).not.toBeNull()
		expect(text()).toContain('港股数据')
	})
})
