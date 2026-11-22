// @vitest-environment jsdom
// 港股人气榜页的口径回归闸 (CONTRIBUTING §3.1 / §4 / §7 / §13):
//   1. 🔴🔴 名次图 y 轴**必须** `inverse: true` —— 名次是「小 = 热门」, 与价格那类
//      「大 = 好」方向相反。不反转的话「人气持续升温」会画成一条一路向下的线,
//      而下降线在所有人直觉里都等于「变差」⇒ 图会系统性传达反的结论。
//   2. 🔴 本页**不得**出现任何价格/涨跌幅(会与看盘/自选/详情形成两个口径, §4),
//      但必须给出通往行情的出口(`openStockPreview`), 否则用户无处可去。
//   3. 🔴 `rank_percentile` 为 null 时渲染「—」而**不是** `0%` —— 0% 会被读成
//      「全市场第一名」。分母(`market_all_count`)缺失时必须显式说明。
//   4. 🔴 tooltip 给**原始** `calc_time`, 轴标签才做裁剪。日频是日期、盘中是时刻,
//      两种格式刻意不统一; 若在 tooltip 里也裁剪, 就等于替上游编了它没说过的精度。
//   5. 🔴 `names_synced=false` 是「榜单有效但名称缺失」, 必须与错误态/空态区分。
//   6. 🔴 `found=false` 是**合法答案**(上游 200 + 全 null), 不得渲染成「加载失败」。
//   7. 🔴 上游失败(502)必须显式报错, **不得**降级成空榜单 —— 「前 100 名一个都
//      没有」会被读成「全市场无人关注」, 那是把故障伪装成结论。
//   8. 🔴 名次变动配色**不用** `text-bull`/`text-bear`: 本项目红绿是涨跌语义,
//      用它表达人气升降会被读成价格方向, 而本页根本不提供价格。
//   9. 🔴 `hk-hot-rank` 前缀**在** `HK_DATA_QUERY_PREFIXES` 里(名称左连本地维表),
//      与南向资金流(不读本地数据, 刻意不在表里)相反 —— 这条钉住那个不对称。
// 九条都是「静默给出误导性结论」级别的缺陷, 肉眼 review 看不出来, 只能用测试钉住。
import {act} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import type {
	HkHotRankDetailResponse, HkHotRankItem, HkHotRankPoint, HkHotRankResponse,
} from '@/lib/api'
import {HK_DATA_QUERY_PREFIXES, QK} from '@/lib/queryKeys'
import {HkHotRank} from './Page'

const h = vi.hoisted(() => ({
	impl: null as null | (() => Promise<unknown>),
	detailImpl: null as null | (() => Promise<unknown>),
	lastDetail: undefined as {symbol: string; withIntraday: boolean} | undefined,
}))

vi.mock('@/lib/api', async importActual => {
	const actual = await importActual<typeof import('@/lib/api')>()
	return {
		...actual,
		api: {
			...actual.api,
			hkHotRank: () => {
				if (!h.impl) throw new Error('测试未配置 hkHotRank 桩')
				return h.impl()
			},
			hkHotRankDetail: (symbol: string, withIntraday = true) => {
				h.lastDetail = {symbol, withIntraday}
				if (!h.detailImpl) throw new Error('测试未配置 hkHotRankDetail 桩')
				return h.detailImpl()
			},
		},
	}
})

// 个股详情宿主的桥: 本页刻意不给价格, 靠它把行情交回单一来源。
const preview = vi.hoisted(() => ({
	openStockPreview: vi.fn(() => true),
	closeStockPreview: vi.fn(),
}))
vi.mock('@/extensions/stockPreview', () => preview)

// echarts 在 jsdom 里没有 canvas, 直接换成桩。
// 🔴 但 setOption 的入参必须留下来: 「y 轴反转」与「tooltip 给原始 calc_time」
// 这两条只能从 option 里查证, DOM 文本看不出来。
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

/** 实测值(2026-11-22): 全市场参与排名 2811 个标的。 */
const MARKET_ALL = 2811
const CALC_TIME = '2026-11-22 13:00:00'
const SOURCE = 'eastmoney_guba_hot_rank'

function pct(rank: number): number {
	return (rank / MARKET_ALL) * 100
}

interface Row {
	rank: number
	symbol: string
	name: string
	rc: number | null
	hrc: number | null
}

/** 实测前 5 名。`rc`/`hrc` 刻意混入 正/负/0/null 四种, 覆盖渲染分支。 */
const ROWS: Row[] = [
	{rank: 1, symbol: '00700.HK', name: '腾讯控股', rc: 0, hrc: 0},
	{rank: 2, symbol: '09988.HK', name: '阿里巴巴-W', rc: 3, hrc: -5},
	{rank: 3, symbol: '01810.HK', name: '小米集团-W', rc: -1, hrc: 12},
	{rank: 4, symbol: '02513.HK', name: '智谱', rc: null, hrc: null},
	{rank: 5, symbol: '01024.HK', name: '快手-W', rc: 7, hrc: 0},
]

function items(): HkHotRankItem[] {
	return ROWS.map(r => ({
		rank: r.rank,
		symbol: r.symbol,
		code: r.symbol.slice(0, 5),
		name: r.name,
		rank_change: r.rc,
		his_rank_change: r.hrc,
		rank_percentile: pct(r.rank),
	}))
}

function payload(over: Partial<HkHotRankResponse> = {}): HkHotRankResponse {
	return {
		items: items(),
		total: ROWS.length,
		market: 'HK',
		market_all_count: MARKET_ALL,
		calc_time: CALC_TIME,
		names_synced: true,
		source: SOURCE,
		max_count: 100,
		...over,
	}
}

/** 日频历史用**日期**, 盘中用**时刻** —— 两种格式刻意不统一(见 `HkHotRankPoint`)。 */
const HISTORY: HkHotRankPoint[] = [
	{calc_time: '2026-07-26', rank: 3},
	{calc_time: '2026-07-27', rank: 2},
	{calc_time: '2026-07-28', rank: 1},
]
const INTRADAY: HkHotRankPoint[] = [
	{calc_time: '2026-11-22 00:00:00', rank: 2},
	{calc_time: '2026-11-22 00:10:00', rank: 1},
]

function detail(over: Partial<HkHotRankDetailResponse> = {}): HkHotRankDetailResponse {
	return {
		symbol: '00700.HK',
		name: '腾讯控股',
		market: 'HK',
		found: true,
		rank: 1,
		rank_change: 0,
		his_rank_change: 0,
		his_rank_change_rank: 1397,
		market_all_count: MARKET_ALL,
		rank_percentile: pct(1),
		calc_time: CALC_TIME,
		history: HISTORY,
		intraday: INTRADAY,
		history_days: HISTORY.length,
		intraday_points: INTRADAY.length,
		source: SOURCE,
		...over,
	}
}

let host: HTMLDivElement | null = null
let root: Root | null = null

beforeEach(() => {
	Object.assign(globalThis, {IS_REACT_ACT_ENVIRONMENT: true})
	h.impl = null
	h.detailImpl = null
	h.lastDetail = undefined
	chart.setOption.mockClear()
	preview.openStockPreview.mockClear()
	host = document.createElement('div')
	document.body.appendChild(host)
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
					<HkHotRank />
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

function rows(): HTMLElement[] {
	return [...host!.querySelectorAll<HTMLElement>('[data-testid="rank-row"]')]
}

/** 「市场分位」列(第 6 列)的单元格文本。 */
function percentileCells(): string[] {
	return rows().map(tr => tr.querySelectorAll('td')[5]?.textContent ?? '')
}

function headerCells(): string[] {
	return [...host!.querySelectorAll('thead th')].map(th => th.textContent ?? '')
}

/** 点第一行进入详情视图。 */
async function openFirstRow(): Promise<void> {
	await act(async () => {
		rows()[0]!.click()
	})
}

interface ChartOpt {
	xAxis?: {data?: string[]}
	yAxis?: {inverse?: boolean; name?: string}
	series?: Array<{name?: string; data?: Array<number | null>}>
	tooltip?: {formatter?: (p: unknown) => string}
}

function chartOpts(): ChartOpt[] {
	return chart.setOption.mock.calls.map(c => c[0] as ChartOpt)
}

/** 按轴标签找出某张图的 option(比依赖 setOption 调用顺序稳)。 */
function optByFirstXLabel(label: string): ChartOpt | undefined {
	return chartOpts().find(o => o.xAxis?.data?.[0] === label)
}

// ===== 1. 🔴🔴 y 轴反转 =====

it('名次图 y 轴 inverse=true(不反转会把「升温」画成「下滑」)', async () => {
	h.impl = async () => payload()
	h.detailImpl = async () => detail()
	await mount()
	await settled(/腾讯控股/)
	await openFirstRow()
	await settled(/日频名次走势/)

	const opts = chartOpts()
	expect(opts.length).toBeGreaterThan(0)
	// 🔴 核心断言: **每一张**名次图都必须反转, 不允许有一张漏掉
	expect(opts.every(o => o.yAxis?.inverse === true)).toBe(true)
})

it('日频与盘中两张图都渲染, 且序列取自各自字段', async () => {
	h.impl = async () => payload()
	h.detailImpl = async () => detail()
	await mount()
	await settled(/腾讯控股/)
	await openFirstRow()
	await settled(/当日盘中名次/)

	// 轴标签按各自格式裁剪: 日频 → MM-DD, 盘中 → HH:mm
	const his = optByFirstXLabel('07-26')
	const intra = optByFirstXLabel('00:00')
	expect(his).toBeTruthy()
	expect(intra).toBeTruthy()
	expect(his!.series?.[0]?.data).toEqual([3, 2, 1])
	expect(intra!.series?.[0]?.data).toEqual([2, 1])
})

// ===== 2. 🔴 无价格, 但有通往行情的出口 =====

it('表头不含任何价格/涨跌幅列', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/腾讯控股/)
	const heads = headerCells()
	for (const bad of ['最新价', '涨跌幅', '现价', '价格', '成交']) {
		expect(heads.some(x => x.includes(bad))).toBe(false)
	}
})

it('每行给出「详情」出口, 点击调 openStockPreview 把行情交回单一来源', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/腾讯控股/)

	const btn = host!.querySelector<HTMLButtonElement>('[aria-label="打开 00700.HK 详情"]')
	expect(btn).not.toBeNull()
	await act(async () => {
		btn!.click()
	})
	expect(preview.openStockPreview).toHaveBeenCalledWith({symbol: '00700.HK', name: '腾讯控股'})
	// 🔴 stopPropagation 必须生效: 点「详情」不该顺带把该行选进详情视图
	expect(rows().length).toBe(ROWS.length)
	expect(h.lastDetail).toBeUndefined()
})

// ===== 3. 🔴 分位 null 不渲染成 0% =====

it('分母缺失时市场分位全渲染「—」, 并显式说明分位不可用', async () => {
	h.impl = async () =>
		payload({
			market_all_count: null,
			items: items().map(it => ({...it, rank_percentile: null})),
		})
	await mount()
	await settled(/腾讯控股/)

	expect(host!.querySelector('[data-testid="no-denominator"]')).not.toBeNull()
	const cells = percentileCells()
	expect(cells.length).toBe(ROWS.length)
	// 🔴 核心断言: 一个都不能是 0 / 0% / 0.00%
	expect(cells.every(c => c === '—')).toBe(true)
})

it('分母存在时按「前 X%」展示, 且第 1 名不是 0%', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/腾讯控股/)
	const cells = percentileCells()
	// 1/2811*100 = 0.0356% ⇒ 两位小数; 🔴 绝不能显示成 0%
	expect(cells[0]).toBe('0.04%')
	expect(cells[0]).not.toBe('0%')
	// 5/2811*100 = 0.1779%
	expect(cells[4]).toBe('0.18%')
	expect(text()).toContain(String(MARKET_ALL))
})

// ===== 4. 🔴 tooltip 给原始 calc_time =====

it('tooltip 输出原始 calc_time, 只有轴标签被裁剪', async () => {
	h.impl = async () => payload()
	h.detailImpl = async () => detail()
	await mount()
	await settled(/腾讯控股/)
	await openFirstRow()
	await settled(/日频名次走势/)

	const intra = optByFirstXLabel('00:00')!
	// 轴标签确实被裁成了 HH:mm
	expect(intra.xAxis?.data?.[0]).toBe('00:00')
	// 🔴 但 tooltip 必须给**完整原始**时刻, 不能是裁剪后的 00:00
	const tip = intra.tooltip?.formatter?.([{dataIndex: 0, value: 2}]) ?? ''
	expect(tip).toContain('2026-11-22 00:00:00')
	expect(tip).toContain('名次 2')

	const his = optByFirstXLabel('07-26')!
	expect(his.xAxis?.data?.[0]).toBe('07-26')
	const tip2 = his.tooltip?.formatter?.([{dataIndex: 2, value: 1}]) ?? ''
	expect(tip2).toContain('2026-07-28')
	expect(tip2).toContain('名次 1')
})

it('日频序列缺 rank 的点保持 null(不补 0)', async () => {
	h.impl = async () => payload()
	h.detailImpl = async () =>
		detail({history: [{calc_time: '2026-07-26', rank: null}, {calc_time: '2026-07-27', rank: 2}]})
	await mount()
	await settled(/腾讯控股/)
	await openFirstRow()
	await settled(/日频名次走势/)
	const his = optByFirstXLabel('07-26')!
	expect(his.series?.[0]?.data).toEqual([null, 2])
})

// ===== 5. 🔴 names_synced=false =====

it('维表未同步: 给出警告, 但榜单照常渲染(不是错误也不是空态)', async () => {
	h.impl = async () =>
		payload({names_synced: false, items: items().map(it => ({...it, name: null}))})
	await mount()
	await settled(/维表未同步/)

	expect(host!.querySelector('[data-testid="names-unsynced"]')).not.toBeNull()
	// 🔴 榜单本身有效: 行数与名次一个不少
	expect(rows().length).toBe(ROWS.length)
	expect(rows()[0]!.getAttribute('data-symbol')).toBe('00700.HK')
	// 名称列渲染占位而不是空白
	expect(rows()[0]!.querySelectorAll('td')[2]?.textContent).toBe('—')
	// 不得被渲染成加载失败
	expect(text()).not.toContain('加载失败')
})

// ===== 6. 🔴 found=false 是合法答案 =====

it('榜外标的渲染「暂不在人气榜体系内」, 不是加载失败', async () => {
	h.impl = async () => payload()
	h.detailImpl = async () => detail({found: false, rank: null, rank_percentile: null})
	await mount()
	await settled(/腾讯控股/)
	await openFirstRow()
	await settled(/暂不在人气榜体系内/)

	expect(host!.querySelector('[data-testid="not-found"]')).not.toBeNull()
	expect(text()).not.toContain('加载失败')
	expect(text()).not.toContain('人气榜加载失败')
})

it('详情查询带上 symbol 与 with_intraday', async () => {
	h.impl = async () => payload()
	h.detailImpl = async () => detail()
	await mount()
	await settled(/腾讯控股/)
	await openFirstRow()
	await settled(/日频名次走势/)
	expect(h.lastDetail).toEqual({symbol: '00700.HK', withIntraday: true})
})

// ===== 7. 🔴 上游失败不降级成空榜单 =====

it('上游 502 渲染错误态, 且不渲染任何榜单行', async () => {
	h.impl = async () => {
		throw new Error('上游返回 502')
	}
	await mount()
	await settled(/人气榜加载失败/)
	expect(text()).toContain('上游返回 502')
	expect(rows()).toHaveLength(0)
	// 🔴 不得出现「暂无数据」这种把故障说成"没有"的文案
	expect(text()).not.toContain('暂无数据')
})

// ===== 8. 🔴 名次变动不用涨跌色 =====

it('名次变动不用 text-bull/text-bear(红绿在本项目是涨跌语义)', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/腾讯控股/)

	const deltas = [
		...host!.querySelectorAll<HTMLElement>('[data-testid="delta-up"], [data-testid="delta-down"]'),
	]
	// 夹具里有 正/负 两种变动, 两个方向都要覆盖到
	expect(deltas.length).toBeGreaterThan(0)
	expect(host!.querySelectorAll('[data-testid="delta-up"]').length).toBeGreaterThan(0)
	expect(host!.querySelectorAll('[data-testid="delta-down"]').length).toBeGreaterThan(0)
	for (const el of deltas) {
		expect(el.className).not.toContain('text-bull')
		expect(el.className).not.toContain('text-bear')
	}
})

it('变动为 0 与 null 分开渲染(0 不是「没有」)', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/腾讯控股/)
	// 第 1 行 rc=0 hrc=0 ⇒ 显示字面 0, 且不带箭头
	const first = rows()[0]!.querySelectorAll('td')
	expect(first[3]?.textContent).toBe('0')
	expect(first[4]?.textContent).toBe('0')
	// 第 4 行 rc=null hrc=null ⇒ 显示占位
	const fourth = rows()[3]!.querySelectorAll('td')
	expect(fourth[3]?.textContent).toBe('—')
	expect(fourth[4]?.textContent).toBe('—')
})

// ===== 9. 🔴 缓存前缀的不对称 =====

it('hk-hot-rank 前缀在 HK_DATA_QUERY_PREFIXES 里(名称左连本地维表)', () => {
	expect(HK_DATA_QUERY_PREFIXES.has(QK.hkHotRank[0])).toBe(true)
	expect(HK_DATA_QUERY_PREFIXES.has(QK.hkHotRankDetail('00700.HK', true)[0])).toBe(true)
	// ⚠️ 与南向资金流**相反**: 那条不读任何本地数据, 刻意不在表里。
	// 这条断言钉住那个不对称 —— 判断标准是「有没有读本地数据」, 不是「是不是港股端点」。
	expect(HK_DATA_QUERY_PREFIXES.has(QK.hkConnectFlow('2026-01-01', '2026-01-02')[0])).toBe(false)
})

it('详情 key 含 symbol 与 withIntraday(两者都影响结果)', () => {
	const a = QK.hkHotRankDetail('00700.HK', true)
	const b = QK.hkHotRankDetail('00700.HK', false)
	const c = QK.hkHotRankDetail('09988.HK', true)
	expect(a).not.toEqual(b)
	expect(a).not.toEqual(c)
	// 🔴 首段必须与列表同前缀, 才能被维表同步一起失效
	expect(a[0]).toBe(QK.hkHotRank[0])
})

// ===== 其余展示契约 =====

it('榜单条数与硬上限同屏展示, 并说明不可翻页', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/腾讯控股/)
	const t = text()
	expect(t).toContain(`${ROWS.length} / 100`)
	// 🔴 必须写明上游静默空的实测结论, 否则后人会来加一个翻页按钮
	expect(t).toContain('不可翻页')
	expect(t).toContain(SOURCE)
})

it('calc_time 原样展示为「榜单重算时刻」, 不冒充交易日', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/腾讯控股/)
	expect(text()).toContain(CALC_TIME)
	expect(text()).toContain('榜单重算时刻')
	// 🔴 不得把它标成「最新交易日」—— 实测非交易日同样有值
	expect(text()).not.toContain('最新交易日')
})

it('过滤框按代码与名称客户端过滤', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/腾讯控股/)
	expect(rows().length).toBe(ROWS.length)

	const input = host!.querySelector<HTMLInputElement>('input[aria-label="过滤代码或名称"]')!
	await act(async () => {
		// React 受控输入: 直接改 value 不会触发 onChange, 需走原生 setter
		const setter = Object.getOwnPropertyDescriptor(
			window.HTMLInputElement.prototype, 'value',
		)!.set!
		setter.call(input, '腾讯')
		input.dispatchEvent(new Event('input', {bubbles: true}))
	})
	await vi.waitFor(async () => {
		await act(async () => {
			await new Promise(resolve => setTimeout(resolve, 0))
		})
		expect(rows().length).toBe(1)
	}, {timeout: 3000})
	expect(rows()[0]!.getAttribute('data-symbol')).toBe('00700.HK')
})

it('过滤无匹配时给出空态提示而不是空白表格', async () => {
	h.impl = async () => payload()
	await mount()
	await settled(/腾讯控股/)
	const input = host!.querySelector<HTMLInputElement>('input[aria-label="过滤代码或名称"]')!
	await act(async () => {
		const setter = Object.getOwnPropertyDescriptor(
			window.HTMLInputElement.prototype, 'value',
		)!.set!
		setter.call(input, 'zzz-不存在')
		input.dispatchEvent(new Event('input', {bubbles: true}))
	})
	await vi.waitFor(async () => {
		await act(async () => {
			await new Promise(resolve => setTimeout(resolve, 0))
		})
		expect(host!.querySelector('[data-testid="table-empty"]')).not.toBeNull()
	}, {timeout: 3000})
	expect(rows()).toHaveLength(0)
})

it('返回榜单按钮回到列表视图', async () => {
	h.impl = async () => payload()
	h.detailImpl = async () => detail()
	await mount()
	await settled(/腾讯控股/)
	await openFirstRow()
	await settled(/日频名次走势/)
	expect(rows()).toHaveLength(0)

	await act(async () => {
		;[...host!.querySelectorAll<HTMLButtonElement>('button')]
			.find(b => (b.textContent ?? '').includes('返回榜单'))!
			.click()
	})
	await settled(/腾讯控股/)
	expect(rows().length).toBe(ROWS.length)
})

it('盘中序列为空时给出可解释的空态(不画空图)', async () => {
	h.impl = async () => payload()
	h.detailImpl = async () => detail({intraday: [], intraday_points: 0})
	await mount()
	await settled(/腾讯控股/)
	await openFirstRow()
	await settled(/非交易时段/)
	expect(host!.querySelector('[data-testid="intraday-chart-empty"]')).not.toBeNull()
	// 日频图仍应正常渲染
	expect(optByFirstXLabel('07-26')).toBeTruthy()
})
