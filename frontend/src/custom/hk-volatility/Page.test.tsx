// @vitest-environment jsdom
// 港股波动率情绪页 (VHSI) 的口径回归闸 (CONTRIBUTING §3.1 / §4 / §7 / §13):
//   1. 🔴🔴 `level` 是**百分点数**不是小数 —— 前端只原样输出, 谁再 ×100 就错 100 倍
//      (24.09 会变成 2409%)。这是本页最容易犯、且肉眼看不出来的缺陷。
//   2. 🔴 `change_pct_1d` 同为百分数, 与项目里 `change_pct` 常见的小数口径**相反**。
//   3. 🔴 档位是**相对**标尺: 分位阈值表与样本三件套必须同屏可见, 否则用户无法
//      查证「今天为什么是偏高」。把阈值埋在代码里等于让人无条件相信一个数字。
//   4. 🔴 样本不足必须 fail closed: 渲染成「样本不足」+ 可操作原因, **不得**落到
//      任何默认档(把「算不出来」伪装成「正常」)。
//   5. 🔴 `corr_hsi` 为 null 时渲染「—」而不是 0 —— 0 会被读成「实测不相关」。
//   6. 🔴 `quote_delay_minutes` 从响应读, 不硬编码 15。
//   7. 🔴 档位配色走**风险递进**刻度而非红涨绿跌: 「恐慌」不得用涨跌语义的红。
//   8. 🔴 `seriesDays` 只裁剪绘图序列, 标定样本与档位不变(后端契约的前端侧证据)。
// 八条都是「静默给出误导性数字」级别的缺陷, 肉眼 review 看不出来, 只能用测试钉住。
import {act} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import type {HkVolatilityBand, HkVolatilityPoint, HkVolatilityResponse} from '@/lib/api'
import {HK_DATA_QUERY_PREFIXES, QK} from '@/lib/queryKeys'
import {HkVolatility} from './Page'

const h = vi.hoisted(() => ({
	impl: null as null | (() => Promise<unknown>),
	lastSeriesDays: undefined as number | undefined,
}))

vi.mock('@/lib/api', async importActual => {
	const actual = await importActual<typeof import('@/lib/api')>()
	return {
		...actual,
		api: {
			...actual.api,
			hkVolatility: (seriesDays?: number) => {
				h.lastSeriesDays = seriesDays
				if (!h.impl) throw new Error('测试未配置 hkVolatility 桩')
				return h.impl()
			},
		},
	}
})

// echarts 在 jsdom 里没有 canvas, 直接换成桩; 断言全部针对 DOM 文本与表格。
// 🔴 但 setOption 的入参要留下来: 「档位分界线画的是 band_upper_levels 而不是
// percentiles」这条只能从 option 里查证。
const chart = vi.hoisted(() => ({
	on: vi.fn(), off: vi.fn(), setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(),
	isDisposed: () => false, getDom: () => null, group: '',
}))
vi.mock('echarts', () => ({init: () => chart, getInstanceByDom: () => null}))
vi.mock('@/lib/theme', () => ({
	useChartTheme: () => ({
		tooltipBg: '#101318', tooltipBorder: '#2a2f38', tooltipText: '#e5e7eb',
		text: '#8b93a1', textStrong: '#d7dbe2', grid: '#1b1f27', border: '#3a4150',
		zoomFill: 'rgba(255,255,255,0.06)',
	}),
}))

/** 后端 `HK_VOLATILITY_BANDS` 的镜像(键序一致)。 */
const BANDS: HkVolatilityBand[] = [
	{upper_percentile: 10, key: 'ultra_low', label: '极低波'},
	{upper_percentile: 35, key: 'low', label: '低波'},
	{upper_percentile: 70, key: 'normal', label: '常态'},
	{upper_percentile: 90, key: 'elevated', label: '偏高'},
	{upper_percentile: 98, key: 'high', label: '高波'},
	{upper_percentile: null, key: 'panic', label: '恐慌'},
]

/** 真实探测值: 2026-11-20 VHSI 收盘 24.09, 前收 21.20 ⇒ +2.89 / +13.632%。 */
const LEVEL = 24.09
const PREV = 21.2

/**
 * 真实抽样分位表(2000 根样本, 2018-10-08 ~ 2026-11-20)。
 * 🔴 刻意让 p50=22.51 **远高于 VIX 的 20**: 若前端或后端任何一处按 VIX 惯例
 * 写死 20/30, 这张表就会自相矛盾, 测试能抓到。
 */
const PERCENTILES: Record<string, number> = {
	p10: 18.3, p25: 19.79, p50: 22.51, p75: 25.03,
	p90: 29.54, p95: 32.18, p98: 37.72, p99: 40.99,
}

/** 档位上界点位: 与 BANDS 同键同序, 末档 null。 */
const BAND_UPPER_LEVELS: Record<string, number | null> = {
	ultra_low: 18.3, low: 20.62, normal: 23.41, elevated: 29.54, high: 37.72, panic: null,
}

function series(n: number): HkVolatilityPoint[] {
	const out: HkVolatilityPoint[] = []
	const base = Date.UTC(2026, 0, 1)
	for (let i = 0; i < n; i++) {
		const d = new Date(base + i * 86_400_000).toISOString().slice(0, 10)
		out.push({date: d, close: 20 + (i % 7), ma20: i >= 19 ? 21.5 : null})
	}
	return out
}

function payload(over: Partial<HkVolatilityResponse> = {}): HkVolatilityResponse {
	return {
		symbol: 'VHSI',
		// 🔴 后端对 VHSI 恒为 null(它不在四个核心指数里), 标题必须由前端给
		name: null,
		market: 'HK',
		currency: 'HKD',
		quote_delay_minutes: 15,
		source: 'local_kline_hk_index_daily',
		level: LEVEL,
		as_of: '2026-11-20',
		change_1d: LEVEL - PREV,
		change_pct_1d: (LEVEL / PREV - 1) * 100,
		percentile: 64.9,
		regime: 'normal',
		regime_label: '常态',
		streak_days: 2,
		moving_averages: {ma5: 20.93, ma20: 20.1, ma60: 19.01},
		percentiles: PERCENTILES,
		band_upper_levels: BAND_UPPER_LEVELS,
		period_high: 64.8,
		period_high_date: '2020-03-16',
		period_low: 12.93,
		period_low_date: '2019-12-23',
		corr_hsi: -0.0814,
		corr_samples: 20,
		corr_window: 20,
		bands: BANDS,
		min_sample_days: 120,
		series: series(250),
		series_days: 250,
		sample_days: 2000,
		sample_start: '2018-10-08',
		sample_end: '2026-11-20',
		insufficient_history: false,
		insufficient_reason: null,
		...over,
	}
}

let root: Root | null = null
let host: HTMLElement | null = null

beforeEach(() => {
	Object.assign(globalThis, {IS_REACT_ACT_ENVIRONMENT: true})
	localStorage.clear()
	h.impl = null
	h.lastSeriesDays = undefined
	chart.setOption.mockClear()
	host = document.createElement('div')
	document.body.append(host)
})

afterEach(() => {
	root?.unmount()
	root = null
	host?.remove()
	host = null
})

/** 挂载页面。retry 关掉, 否则失败路径要等指数退避。 */
async function mount(): Promise<HTMLElement> {
	const client = new QueryClient({
		defaultOptions: {queries: {retry: false, staleTime: Infinity, gcTime: Infinity}},
	})
	root = createRoot(host!)
	await act(async () => {
		root!.render(
			<QueryClientProvider client={client}>
				<MemoryRouter future={{v7_startTransition: true, v7_relativeSplatPath: true}}>
					<HkVolatility />
				</MemoryRouter>
			</QueryClientProvider>,
		)
	})
	return host!
}

/** react-query 的通知批处理需要多个微任务轮次, act 单次空 flush 不够 — 轮询等待。 */
async function settled(marker: string | RegExp): Promise<void> {
	await vi.waitFor(async () => {
		await act(async () => {
			await new Promise(resolve => setTimeout(resolve, 0))
		})
		expect(text()).toMatch(marker)
	}, {timeout: 3000})
}

function text(): string {
	return host?.textContent ?? ''
}

/** 分位阈值表那一行的单元格文本(键序 = P10…P99)。 */
function percentileCells(): string[] {
	const tr = host!.querySelector<HTMLTableRowElement>('[data-testid="percentile-row"]')
	if (!tr) return []
	return [...tr.querySelectorAll('td')].map(td => td.textContent ?? '')
}

/** 档位徽标。 */
function regimeChip(): HTMLElement | null {
	return host!.querySelector<HTMLElement>('[data-testid="regime-chip"]')
}

/** 从 setOption 入参里取出 VHSI 系列的 markLine 数据(档位分界线)。 */
function markLines(): number[] {
	interface SeriesOpt {
		name?: string
		markLine?: {data?: Array<{yAxis: number}>}
	}
	const opt = chart.setOption.mock.calls.at(-1)?.[0] as {series?: SeriesOpt[]} | undefined
	const s = opt?.series?.find(x => x.name === 'VHSI')
	return (s?.markLine?.data ?? []).map(m => m.yAxis)
}

// ===== 1. 单位: 百分点数, 绝不 ×100 =====

it('level 原样输出为百分点数, 不被 ×100', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/常态/)
	const t = text()
	// 🔴 核心断言: 24.09 出现, 而 2409 / 2409.00 绝不出现
	expect(t).toContain('24.09')
	expect(t).not.toContain('2409')
	expect(t).not.toContain('0.2409')
})

it('change_pct_1d 已是百分数, 只拼 % 与符号', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/常态/)
	// +13.63% (13.632 四舍五入), 不是 +1363.20% 也不是 +0.14%
	expect(text()).toContain('+13.63%')
	expect(text()).not.toContain('1363')
	// 点位差同样原样: +2.89
	expect(text()).toContain('+2.89')
})

it('分位阈值表原样输出点位, 不被 ×100', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/常态/)
	const cells = percentileCells()
	expect(cells).toHaveLength(Object.keys(PERCENTILES).length)
	expect(cells[0]).toBe('18.30') // p10
	expect(cells[2]).toBe('22.51') // p50
	expect(text()).not.toContain('1830')
})

// ===== 2. 档位是相对标尺: 阈值与样本必须可见 =====

it('分位阈值表与标定样本三件套同屏可见', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/常态/)
	const t = text()
	// 样本三件套: 天数 + 起止
	expect(t).toContain('2000')
	expect(t).toContain('2018-10-08')
	expect(t).toContain('2026-11-20')
	// 阈值表全部八档都在
	for (const v of Object.values(PERCENTILES)) expect(t).toContain(v.toFixed(2))
	// 明确写出「不是 VIX 的 20/30」, 防止读者按美股惯例解读
	expect(t).toMatch(/VIX/)
})

it('档位定义列出每一档的分位上界与对应点位', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/常态/)
	const t = text()
	for (const b of BANDS) expect(t).toContain(b.label)
	// 末档标注「无上界」而不是留空
	expect(t).toContain('无上界')
	// 有上界的档给出「< pNN (点位)」
	expect(t).toContain('< p35')
	expect(t).toContain(BAND_UPPER_LEVELS.low!.toFixed(2))
})

it('图表的档位分界线取自 band_upper_levels 而非 percentiles', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/常态/)
	// 🔴 markLines() 返回的已是 yAxis 数值数组(见 helper), 这里不要再 map 一次
	const lines = markLines()
	// 🔴 p35/p70 不在 percentiles 里, 只有 band_upper_levels 有 ⇒ 这条断言
	// 能抓住「前端拿 bands[].upper_percentile 去 percentiles 里查」的错误实现
	// (那样会静默少画 low/normal 两条线)。
	expect(lines).toContain(BAND_UPPER_LEVELS.low) // 20.62 — percentiles 里没有
	expect(lines).toContain(BAND_UPPER_LEVELS.normal) // 23.41 — percentiles 里没有
	expect(lines).toContain(BAND_UPPER_LEVELS.elevated)
	expect(lines).toContain(BAND_UPPER_LEVELS.high)
	// 末档无上界 ⇒ 不画线
	expect(lines).toHaveLength(BANDS.filter(b => b.upper_percentile != null).length)
})

// ===== 3. 样本不足 fail closed =====

it('样本不足时渲染原因, 不落到任何默认档', async () => {
	h.impl = async () => payload({
		insufficient_history: true,
		insufficient_reason: '本地 VHSI 历史仅 40 个交易日, 不足以标定分位阈值(需 ≥120)',
		regime: null, regime_label: null, percentile: null,
		streak_days: null, corr_hsi: null, corr_samples: 0,
		percentiles: {}, band_upper_levels: {},
		moving_averages: {ma5: null, ma20: null, ma60: null},
		sample_days: 40, series: series(40), series_days: 40,
	})
	await mount()
	await settled(/样本不足/)
	const t = text()
	expect(t).toContain('本地 VHSI 历史仅 40 个交易日')
	// 🔴 不得出现任何档位名 —— 那等于把「算不出来」说成「常态」
	expect(regimeChip()).toBeNull()
	for (const b of BANDS) expect(t).not.toContain(b.label)
	// 给出可操作的补救路径与门槛
	expect(t).toContain('120')
	expect(t).toMatch(/sync_index_daily|数据/)
	// 客观事实照常给: 最新点位与样本内极值
	expect(t).toContain(LEVEL.toFixed(2))
})

it('样本不足但有点位时仍显示最新点位与极值日期', async () => {
	h.impl = async () => payload({
		insufficient_history: true,
		insufficient_reason: '样本不足',
		regime: null, regime_label: null, percentile: null, streak_days: null,
		corr_hsi: null, percentiles: {}, band_upper_levels: {},
		sample_days: 10,
	})
	await mount()
	await settled(/样本不足/)
	const t = text()
	expect(t).toContain('2020-03-16') // period_high_date
	expect(t).toContain('64.80')
})

// ===== 4. null 与 0 必须渲染成不同的东西 =====

it('corr_hsi 为 null 时渲染破折号而不是 0', async () => {
	h.impl = async () => payload({corr_hsi: null, corr_samples: 0})
	await mount()
	await settled(/常态/)
	const t = text()
	expect(t).toContain('—')
	// 🔴 不得出现 0.000 (那会被读成「实测不相关」)
	expect(t).not.toContain('0.000')
	// 且提示里说明为什么是空的。
	// 🔴 hint 渲染成 title 属性(悬浮提示), **不在 textContent 里** ⇒ 必须查属性。
	const corrCard = [...host!.querySelectorAll<HTMLDivElement>('div[title]')]
		.find(el => (el.textContent ?? '').includes('与恒指相关性'))!
	expect(corrCard.getAttribute('title')).toMatch(/基准|缺失|方差/)
})

it('corr_hsi 有值时按三位小数渲染', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/常态/)
	expect(text()).toContain('-0.081')
})

it('均线为 null 时渲染破折号而不是 0', async () => {
	h.impl = async () => payload({moving_averages: {ma5: null, ma20: 20.1, ma60: null}})
	await mount()
	await settled(/常态/)
	const t = text()
	expect(t).toContain('20.10')
	// MA5 / MA60 位置是 —
	expect(t.match(/—/g)!.length).toBeGreaterThanOrEqual(2)
})

// ===== 5. 延迟分钟数从响应读 =====

it('行情延迟分钟数取自响应而非硬编码', async () => {
	h.impl = async () => payload({quote_delay_minutes: 30})
	await mount()
	await settled(/常态/)
	expect(text()).toContain('行情延迟 30 分钟')
	expect(text()).not.toContain('行情延迟 15 分钟')
})

// ===== 6. 档位配色不用涨跌语义色 =====

it('恐慌档不使用涨跌语义的红/绿 class', async () => {
	h.impl = async () => payload({regime: 'panic', regime_label: '恐慌', percentile: 99.5})
	await mount()
	await settled(/恐慌/)
	const chip = regimeChip()!
	const cls = chip.className
	// 🔴 风险递进刻度: 允许 danger(风险色), 但**不得**出现 bull/bear(涨跌色)
	expect(cls).not.toContain('text-bull')
	expect(cls).not.toContain('text-bear')
	expect(cls).not.toContain('bg-bull')
	expect(cls).not.toContain('bg-bear')
})

it('未知档位键回落到中性样式而不崩溃', async () => {
	h.impl = async () => payload({regime: 'brand_new_band', regime_label: '新档位'})
	await mount()
	await settled(/新档位/)
	const chip = regimeChip()!
	expect(chip.className).toContain('text-muted')
	expect(chip.className).not.toContain('text-bull')
})

// ===== 7. seriesDays 只裁剪绘图序列 =====

it('切换绘图区间只改 series_days, 标定样本与档位不变', async () => {
	const full = payload()
	h.impl = async () => full
	await mount()
	await settled(/常态/)
	expect(h.lastSeriesDays).toBe(250)

	// 换成「近 90 天」: 后端契约是档位/阈值/样本三件套全部不变
	h.impl = async () => payload({series: series(90), series_days: 90})
	const btn = [...host!.querySelectorAll<HTMLButtonElement>('button')]
		.find(b => (b.textContent ?? '').trim() === '近 90 天')!
	await act(async () => { btn.click() })
	await settled(/常态/)

	expect(h.lastSeriesDays).toBe(90)
	const t = text()
	// 🔴 标定样本仍是 2000, 档位仍是常态, 阈值表一个数都没变
	expect(t).toContain('2000')
	expect(t).toContain('常态')
	for (const v of Object.values(PERCENTILES)) expect(t).toContain(v.toFixed(2))
	// 页面上明确写出这条口径, 免得用户以为切区间会改判定
	expect(t).toMatch(/只裁剪绘图序列/)
})

// ===== 8. 错误态与空态 =====

it('请求失败渲染错误态而不是空态', async () => {
	h.impl = async () => { throw new Error('boom') }
	await mount()
	await settled(/加载失败/)
	expect(text()).toContain('boom')
})

it('后端返回空样本时渲染样本不足而不是崩溃', async () => {
	h.impl = async () => payload({
		insufficient_history: true,
		insufficient_reason: '本地没有 VHSI 日K数据',
		level: null, as_of: null, change_1d: null, change_pct_1d: null,
		regime: null, regime_label: null, percentile: null, streak_days: null,
		corr_hsi: null, percentiles: {}, band_upper_levels: {},
		moving_averages: {ma5: null, ma20: null, ma60: null},
		period_high: null, period_high_date: null,
		period_low: null, period_low_date: null,
		sample_days: 0, series: [], series_days: 0,
		sample_start: null, sample_end: null,
	})
	await mount()
	await settled(/样本不足/)
	expect(text()).toContain('本地没有 VHSI 日K数据')
})

// ===== 9. query key 归属 =====

it('hk-volatility 前缀在 HK_DATA_QUERY_PREFIXES 里(同步后必须失效)', () => {
	// 🔴 它读本地 kline_hk_index_daily: 港股指数同步完不失效, 就会留着按旧样本
	// 标定的分位阈值 —— 阈值一变档位可能整个换档, 界面上却看不出差别。
	expect(HK_DATA_QUERY_PREFIXES.has(QK.hkVolatility(250)[0])).toBe(true)
})

it('seriesDays 入 query key(否则改区间不重新取数)', () => {
	expect(QK.hkVolatility(90)).not.toEqual(QK.hkVolatility(250))
	expect(QK.hkVolatility(250)).toEqual(['hk-volatility', 250])
})
