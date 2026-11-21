// @vitest-environment jsdom
// 港股通(南向)资金流的口径回归闸 (CONTRIBUTING §3.1 / §4):
//   1. 后端给的金额单位是**元**, 前端只做展示级亿/万亿缩写 —— 谁再乘除一次就错 1e6 倍;
//   2. null(算不出来) 与 0(确实没有净买入) 必须渲染成不同的东西;
//   3. index_change_pct 已是**百分比数值**, 只拼 '%' —— 再乘 100 就错 100 倍;
//   4. 上游 502 必须显示成**故障**, 不能落进「该窗口内没有交易日」的空态;
//   5. 页面不得出现持股市值 —— 上游该列口径无法定性, 后端已在数据源层排除。
// 五条都是「静默给出误导性数字」级别的缺陷, 肉眼 review 看不出来, 只能用测试钉住。
import {act} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {ApiError, type HkConnectFlowResponse, type HkConnectFlowRow} from '@/lib/api'
import {HK_DATA_QUERY_PREFIXES, QK} from '@/lib/queryKeys'
import {HkConnectFlow, fmtNetYuan, fmtYuan} from './Page'

const h = vi.hoisted(() => ({impl: null as null | (() => Promise<unknown>)}))

vi.mock('@/lib/api', async importActual => {
	const actual = await importActual<typeof import('@/lib/api')>()
	return {
		...actual,
		api: {
			...actual.api,
			hkConnectFlow: () => {
				if (!h.impl) throw new Error('测试未配置 hkConnectFlow 桩')
				return h.impl()
			},
		},
	}
})

// echarts 在 jsdom 里没有 canvas, 直接换成桩; 本文件的断言全部针对 DOM 文本与表格
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

/** 真实抽样: 2026-11-20 南向合计净买入 11811.50 百万元 ⇒ 11,811,500,000 元。 */
function row(over: Partial<HkConnectFlowRow>): HkConnectFlowRow {
	return {
		trade_date: '2026-11-20',
		net_buy: 11_811_500_000,
		buy: 54_746_570_000,
		sell: 42_935_070_000,
		turnover: 97_681_640_000,
		accum_net_inflow: 5_534_878_670_000,
		ggt_sh_net_buy: 6_562_380_000,
		ggt_sz_net_buy: 5_249_120_000,
		ggt_sh_turnover: 56_017_040_000,
		ggt_sz_turnover: 41_664_600_000,
		index_close: 22410.8,
		index_change_pct: -1.89,
		...over,
	}
}

function payload(items: HkConnectFlowRow[], over: Partial<HkConnectFlowResponse> = {}) {
	const newest = items.length ? items[items.length - 1] : null
	return {
		items,
		total: items.length,
		start: '2026-08-22',
		end: '2026-11-20',
		market: 'HK',
		currency: 'CNY',
		source: 'eastmoney_datacenter_hsgt',
		trading_days: items.length,
		latest_trade_date: newest?.trade_date ?? null,
		latest_net_buy: newest?.net_buy ?? null,
		accum_net_inflow: newest?.accum_net_inflow ?? null,
		net_buy_5d: 16_497_890_000,
		net_buy_20d: 14_092_780_000,
		index_close: newest?.index_close ?? null,
		index_change_pct: newest?.index_change_pct ?? null,
		...over,
	} satisfies HkConnectFlowResponse
}

let root: Root | null = null
let host: HTMLElement | null = null

beforeEach(() => {
	Object.assign(globalThis, {IS_REACT_ACT_ENVIRONMENT: true})
	localStorage.clear()
	h.impl = null
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
					<HkConnectFlow />
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

// ── 展示缩写: 元 → 万/亿/万亿, 且负数不能被 fmtBigNum 那套 `v >= 1e8` 判断漏掉 ──

it('fmtYuan 对正负数都按绝对值选单位', () => {
	// 118.115 在 IEEE754 里存成 118.1149999…, 故 toFixed(2) 得 118.11 而非 118.12。
	// 这是全仓库一致的 toFixed 行为(fmtBigNum 同理), 差 0.005亿 = 50万元, 不影响判读,
	// 不要为了「看起来更准」在这里单独引入一套舍入。
	expect(fmtYuan(11_811_500_000)).toBe('118.11亿')
	// 🔴 净流出必须同样缩写; 复用 lib/format 的 fmtBigNum 会得到 '-11811500000'
	expect(fmtYuan(-11_811_500_000)).toBe('-118.11亿')
	expect(fmtYuan(5_534_878_670_000)).toBe('5.53万亿')
	expect(fmtYuan(-5_534_878_670_000)).toBe('-5.53万亿')
	expect(fmtYuan(56_017_040)).toBe('5602万')
	expect(fmtYuan(999)).toBe('999')
	expect(fmtYuan(null)).toBe('—')
	expect(fmtYuan(undefined)).toBe('—')
})

it('fmtNetYuan 给正值加显式 +, 让流入/流出扫视可辨', () => {
	expect(fmtNetYuan(11_811_500_000)).toBe('+118.11亿')
	expect(fmtNetYuan(-11_811_500_000)).toBe('-118.11亿')
	// 0 不加号: 「+0.00」会让人以为有方向
	expect(fmtNetYuan(0)).toBe('0')
	expect(fmtNetYuan(null)).toBe('—')
})

it('金额按元渲染成亿/万亿缩写, 不再乘除任何倍数', async () => {
	h.impl = async () => payload([row({})])
	await mount()
	await settled('118.11亿')
	const t = text()
	// 🔴 后端已在数据源边界把「百万元」换成「元」; 前端再除一次会得到 0.01亿
	expect(t).toContain('+118.11亿'), '净买入 11,811,500,000 元应显示 +118.11亿'
	expect(t).toContain('5.53万亿'), '累计净流入应显示 5.53万亿'
	expect(t).not.toContain('11811500000'), '不得出现未缩写的原始数字'
	expect(t).not.toContain('0.01亿'), '不得把元当成百万元再除一次'
	expect(t).not.toContain('11811.50亿'), '不得把元当成百万元再乘一次'
})

it('index_change_pct 当百分比数值渲染, 不再乘 100', async () => {
	h.impl = async () => payload([row({index_change_pct: -1.89})])
	await mount()
	await settled('22410.80')
	const t = text()
	expect(t).toContain('-1.89%')
	// 🔴 乘了 100 会得到 -189.00%
	expect(t).not.toContain('-189.00%')
	expect(t).not.toContain('-0.02%')
})

it('滚动合计为 null 时渲染「—」, 不渲染 0 也不渲染部分和', async () => {
	// 窗口只有 3 个交易日 ⇒ 后端刻意不给 5 日/20 日合计(3 天的和冒充「近 5 日」看不出来)
	h.impl = async () =>
		payload(
			[
				row({trade_date: '2026-11-18', net_buy: 1_000_000_000}),
				row({trade_date: '2026-11-19', net_buy: 2_000_000_000}),
				row({trade_date: '2026-11-20', net_buy: 3_000_000_000}),
			],
			{trading_days: 3, net_buy_5d: null, net_buy_20d: null},
		)
	await mount()
	await settled('2026-11-20')
	const t = text()
	expect(t).toContain('近 5 日合计')
	expect(t).toContain('近 20 日合计')
	expect(t).toContain('—'), '算不出来的合计必须是「—」'
	// 🔴 关键: 不得把 3 天的部分和 (6,000,000,000 = 60.00亿) 冒充近 5 日
	expect(t).not.toContain('60.00亿')
	expect(t).not.toContain('+0.00')
})

it('深港通开通前港股通(深)为 null 时渲染「—」而非 0', async () => {
	h.impl = async () =>
		payload([
			row({
				trade_date: '2016-12-02',
				ggt_sz_net_buy: null,
				ggt_sz_turnover: null,
				net_buy: 6_562_380_000,
			}),
		])
	await mount()
	await settled('2016-12-02')
	const tr = [...host!.querySelectorAll('tbody tr')].find(r => r.textContent?.includes('2016-12-02'))!
	const cells = [...tr.querySelectorAll('td')].map(td => td.textContent)
	// 列序: 交易日 净买入 买入 卖出 成交额 港股通(沪) 港股通(深) 累计 恒指 涨跌
	expect(cells[6]).toBe('—'), '港股通(深)净买入缺数必须是「—」'
	expect(cells[5]).toBe('+65.62亿'), '港股通(沪)照常显示'
})

it('上游 502 显示成数据源故障, 而不是「没有交易日」', async () => {
	h.impl = async () => {
		throw new ApiError('港股通资金流数据源不可用: datacenter 报表失败', 502)
	}
	await mount()
	await settled('港股通资金流数据源不可用')
	const t = text()
	expect(t).toContain('港股通资金流数据源不可用')
	// 🔴 关键: 故障不得伪装成合法的空结果
	expect(t).not.toContain('该窗口内没有交易日')
})

it('窗口内确实没有交易日时给出空态(与故障可区分)', async () => {
	h.impl = async () =>
		payload([], {
			trading_days: 0,
			latest_trade_date: null,
			latest_net_buy: null,
			accum_net_inflow: null,
			net_buy_5d: null,
			net_buy_20d: null,
			index_close: null,
			index_change_pct: null,
		})
	await mount()
	await settled('该窗口内没有交易日')
	const t = text()
	expect(t).toContain('该窗口内没有交易日')
	expect(t).not.toContain('港股通资金流数据源不可用')
})

it('币种与数据源在页面上可见, 且明示币种是推断而非上游声明', async () => {
	h.impl = async () => payload([row({})])
	await mount()
	await settled('eastmoney_datacenter_hsgt')
	const t = text()
	expect(t).toContain('CNY')
	expect(t).toContain('eastmoney_datacenter_hsgt')
	expect(t).toContain('推断'), '该报表无币种列, 必须说明 CNY 是推断出来的'
})

it('页面不提供持股市值 —— 上游该列口径无法定性, 已在数据源层排除', async () => {
	h.impl = async () => payload([row({})])
	await mount()
	await settled('118.11亿')
	// 🔴 上游 HOLD_MARKET_CAP 有两种相差 2 倍的解释且无独立数据源可验证;
	// 前端「补一个进来」等于把后端的谨慎决定推翻。
	// 断言打在**表头与概览卡片**上而不是整页文本: 脚注里那句「本页不提供持股市值」
	// 是刻意保留的解释, 它本身就含这四个字。
	const headers = [...host!.querySelectorAll('thead th')].map(th => th.textContent ?? '')
	expect(headers.some(t => t.includes('持股市值'))).toBe(false)
	expect(headers.some(t => t.includes('持股'))).toBe(false)
	const cards = [...host!.querySelectorAll('section .truncate')].map(el => el.textContent ?? '')
	expect(cards.some(t => t.includes('持股'))).toBe(false)
	// 但必须**说明**为什么没有, 否则下一个人会以为是漏做了
	expect(text()).toContain('不提供持股市值')
})

it('窗口超过后端硬闸时不发请求, 直接给出可操作的原因', async () => {
	let called = 0
	h.impl = async () => {
		called += 1
		return payload([row({})])
	}
	await mount()
	await settled('118.11亿')
	expect(called).toBe(1)

	// 把起始日推到 6 年前 ⇒ 跨度 > 1825 天
	const startInput = host!.querySelector<HTMLInputElement>('input[aria-label="起始交易日"]')!
	await act(async () => {
		const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
		setter.call(startInput, '2015-01-01')
		startInput.dispatchEvent(new Event('input', {bubbles: true}))
		startInput.dispatchEvent(new Event('change', {bubbles: true}))
	})
	await vi.waitFor(() => expect(text()).toContain('窗口不合法'))
	expect(called, '非法窗口不得打上游(后端会 400, 但没必要吃这个错)').toBe(1)
	expect(text()).toContain('上限 1825 天')
})

it('查询 key 带上窗口两端, 改日期不会被旧缓存顶掉', () => {
	// 数据不落库, 缓存就是唯一状态; key 漏了日期会让「换了窗口还是上一窗口的行」
	const a = QK.hkConnectFlow('2026-08-22', '2026-11-20')
	const b = QK.hkConnectFlow('2026-05-22', '2026-11-20')
	expect(a[0]).toBe('hk-connect-flow')
	expect(a).not.toEqual(b)
	// 🔴 刻意不在 HK_DATA_QUERY_PREFIXES 里: 本页不读任何本地数据, 同步完再刷只是白打一次上游
	expect(HK_DATA_QUERY_PREFIXES.has(a[0])).toBe(false)
	// 对照: AH 溢价的 H 侧价格取自本地日K分区, 必须在集合里
	expect(HK_DATA_QUERY_PREFIXES.has(QK.hkAhPremium[0])).toBe(true)
})

it('明细表最新交易日在最上面', async () => {
	h.impl = async () =>
		payload([
			row({trade_date: '2026-11-18'}),
			row({trade_date: '2026-11-19'}),
			row({trade_date: '2026-11-20'}),
		])
	await mount()
	await settled('2026-11-20')
	const dates = [...host!.querySelectorAll('tbody tr')].map(tr => tr.querySelector('td')?.textContent)
	expect(dates).toEqual(['2026-11-20', '2026-11-19', '2026-11-18'])
})

it('图表拿到的是后端原始升序序列与元为单位的数值', async () => {
	h.impl = async () =>
		payload([
			row({trade_date: '2026-11-19', net_buy: -3_958_920_000, index_close: 22841.62}),
			row({trade_date: '2026-11-20', net_buy: 11_811_500_000, index_close: 22410.8}),
		])
	await mount()
	await settled('2026-11-20')
	const opt = chart.setOption.mock.calls.at(-1)![0] as {
		xAxis: {data: string[]}
		series: Array<{name: string; data: Array<{value: number | null; itemStyle?: {color: string}} | number | null>}>
	}
	expect(opt.xAxis.data).toEqual(['2026-11-19', '2026-11-20'])
	const bars = opt.series[0].data as Array<{value: number | null; itemStyle: {color: string}}>
	// 🔴 传给图表的必须是**元**, 缩写只发生在 axisLabel/tooltip 的 formatter 里
	expect(bars[0].value).toBe(-3_958_920_000)
	expect(bars[1].value).toBe(11_811_500_000)
	expect(bars[0].itemStyle.color).toBe('#12B76A'), '净流出用绿'
	expect(bars[1].itemStyle.color).toBe('#F04438'), '净流入用红'
	const line = opt.series[1].data as Array<number | null>
	expect(line).toEqual([22841.62, 22410.8])
})

it('上游缺合计行时净买入与成交额为「—」, 沪深分通道仍照实显示', async () => {
	// 上游某日缺 006 合计行 ⇒ net_buy 为 null (后端刻意不用沪+深兜底)
	h.impl = async () =>
		payload([
			row({trade_date: '2026-11-19', net_buy: null, turnover: null}),
			row({trade_date: '2026-11-20'}),
		])
	await mount()
	await settled('2026-11-20')
	const tr = [...host!.querySelectorAll('tbody tr')].find(r => r.textContent?.includes('2026-11-19'))!
	const cells = [...tr.querySelectorAll('td')].map(td => td.textContent)
	expect(cells[1]).toBe('—'), '合计行缺失时净买入必须是「—」'
	expect(cells[4]).toBe('—'), '合计行缺失时成交额必须是「—」'
	// 沪深分通道仍有值 ⇒ 照实显示, 但不拿来冒充合计
	expect(cells[5]).toBe('+65.62亿')
})
