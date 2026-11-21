// @vitest-environment jsdom
// AH 溢价页的口径回归闸 (CONTRIBUTING §3.1 / §4 / §13):
//   1. 🔴🔴 汇率(值 / 数据日期 / 来源)必须**常驻可见**, 不折叠、不需要交互 ——
//      这是本指标作为「仓库唯一被允许做汇率换算的例外」的第四个前提条件;
//   2. premium_pct 已是**百分比数值**, 前端只拼 '%' —— 谁再乘 100 就错 100 倍;
//   3. null(算不出来) 与 0(A/H 恰好同价) 必须渲染成不同的东西;
//   4. 四个数据质量计数 >0 时必须可见, 歧义丢弃还要点名到具体代码;
//   5. 上游 502 必须显示成**故障**, 不能落进「配对表返回空」的空态;
//   6. A 收盘(CNY)与 H 收盘(HKD)是两种币种, 不得混排成看起来可直接相除的样子。
// 六条都是「静默给出误导性数字」级别的缺陷, 肉眼 review 看不出来, 只能用测试钉住。
import {act} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {QueryClient, QueryClientProvider} from '@tanstack/react-query'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {ApiError, type HkAhPremiumResponse, type HkAhPremiumRow} from '@/lib/api'
import {HK_DATA_QUERY_PREFIXES, QK} from '@/lib/queryKeys'
import {HkAhPremium} from './Page'

const h = vi.hoisted(() => ({impl: null as null | (() => Promise<unknown>)}))

vi.mock('@/lib/api', async importActual => {
	const actual = await importActual<typeof import('@/lib/api')>()
	return {
		...actual,
		api: {
			...actual.api,
			hkAhPremium: () => {
				if (!h.impl) throw new Error('测试未配置 hkAhPremium 桩')
				return h.impl()
			},
		},
	}
})

/** 真实探测值: 2026-11-21 新浪 fx_shkdcny。 */
const FX_RATE = 0.851970181

/**
 * 真实抽样行。溢价率按后端公式 A ÷ (H × 汇率) − 1 现算, 不写死:
 * 写死一个「看起来对」的数就等于把公式的正确性假设进测试里。
 */
function premiumOf(aClose: number, hClose: number, fx: number = FX_RATE): number {
	return (aClose / (hClose * fx) - 1) * 100
}

function row(over: Partial<HkAhPremiumRow>): HkAhPremiumRow {
	return {
		a_symbol: '601398.SH',
		a_name: '工商银行',
		h_symbol: '01398.HK',
		h_name: '工商银行',
		a_close: 7.28,
		a_price_date: '2026-11-20',
		h_close: 6.79,
		h_price_date: '2026-11-20',
		premium_pct: premiumOf(7.28, 6.79),
		...over,
	}
}

function payload(items: HkAhPremiumRow[], over: Partial<HkAhPremiumResponse> = {}) {
	return {
		items,
		total: items.length,
		currency: 'CNY',
		source: 'eastmoney_datacenter_f10_orginfo+local_kline',
		fx_rate: FX_RATE,
		fx_as_of: '2026-11-21',
		fx_source: 'sina_hq_fx',
		candidate_count: items.length,
		missing_price_count: items.filter(i => i.a_close == null || i.h_close == null).length,
		stale_price_count: 0,
		price_lag_count: 0,
		ambiguous_a_symbols: [],
		...over,
	} satisfies HkAhPremiumResponse
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
					<HkAhPremium />
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

/** 重新挂载一次(带新的桩响应)。「计数为 0 时不打扰 / >0 时可见」需要两轮。 */
async function remount(): Promise<void> {
	root?.unmount()
	host?.remove()
	host = document.createElement('div')
	document.body.append(host)
	await mount()
}

function bodyRows(): HTMLTableRowElement[] {
	return [...host!.querySelectorAll<HTMLTableRowElement>('tbody tr')]
}

/** 列序: A代码 A名称 A收盘 A价格日 H代码 H名称 H收盘 H价格日 溢价率 */
function cells(tr: HTMLTableRowElement): string[] {
	return [...tr.querySelectorAll('td')].map(td => td.textContent ?? '')
}

/**
 * 只取「数据质量计数」那几个徽标(QualityFlag 带 `cursor-help`)。
 * 🔴 不能拿整页文本断言「不出现『日期错位』」—— 脚注里那句解释本身就含这四个字,
 * 断言会自相矛盾。要钉住的是「计数为 0 时不渲染徽标」这个行为。
 */
function qualityFlags(): string[] {
	return [...host!.querySelectorAll('span.cursor-help')].map(el => el.textContent ?? '')
}

/** 表格数据行(排除空态那一行 colSpan 占位)。 */
function dataRows(): HTMLTableRowElement[] {
	return bodyRows().filter(tr => tr.querySelectorAll('td').length > 1)
}

// ── 🔴🔴 第四根支柱: 汇率必须常驻可见 ──

it('汇率值 / 数据日期 / 来源常驻显示, 无需任何交互', async () => {
	h.impl = async () => payload([row({})])
	await mount()
	await settled('601398.SH')
	const t = text()
	expect(t).toContain('港币汇率')
	expect(t).toContain(FX_RATE.toFixed(6)), '汇率必须显示到 6 位小数, 截断会让复算对不上'
	expect(t).toContain('2026-11-21'), '汇率的数据日期必须可见'
	expect(t).toContain('sina_hq_fx'), '汇率来源必须可见'
	expect(t).toContain('CNY / HKD'), '必须标明方向, 0.85 是 CNY/HKD 还是 HKD/CNY 差 1.35 倍'
	// 🔴 不得藏进需要点击/悬停才出现的结构里
	const banner = host!.querySelector('section')!
	expect(banner.textContent).toContain(FX_RATE.toFixed(6))
	expect(banner.querySelector('details')).toBeNull()
	expect(banner.querySelector('button')).toBeNull()
})

it('汇率取不到时后端 502, 前端显示成故障而不是用 1.0 兜底', async () => {
	h.impl = async () => {
		throw new ApiError('港币汇率数据源不可用: 三角校验偏差超限', 502)
	}
	await mount()
	await settled('AH 溢价数据源不可用')
	const t = text()
	expect(t).toContain('港币汇率数据源不可用')
	// 🔴 关键: 用 1.0 当汇率会让每一只标的的溢价都错约 17%, 且从数字上看不出来
	expect(t).not.toContain('1.000000')
	expect(t).not.toContain('上游配对表返回空')
	expect(t).toContain('用 1.0 当汇率兜底')
})

it('配对表 502 与汇率 502 是两条不同的消息, 不得合并', async () => {
	h.impl = async () => {
		throw new ApiError('AH 配对数据源不可用: datacenter 报表失败', 502)
	}
	await mount()
	await settled('AH 配对数据源不可用')
	expect(text()).toContain('AH 配对数据源不可用')
	expect(text()).not.toContain('港币汇率数据源不可用')
})

// ── 百分比与空值 ──

it('premium_pct 当百分比数值渲染, 不再乘 100', async () => {
	// 真实榜首: 000488.SZ ST晨鸣 A=1.6 / 01812.HK 晨鸣纸业 H=0.32 ⇒ 486.875(%)
	const p = premiumOf(1.6, 0.32)
	expect(p).toBeCloseTo(486.875, 6)
	h.impl = async () =>
		payload([
			row({
				a_symbol: '000488.SZ', a_name: 'ST晨鸣', a_close: 1.6,
				h_symbol: '01812.HK', h_name: '晨鸣纸业', h_close: 0.32, premium_pct: p,
			}),
		])
	await mount()
	await settled('000488.SZ')
	const t = text()
	expect(t).toContain('+486.88%')
	// 🔴 乘了 100 会得到 48687.50%; 当成小数直接拼会得到 4.87%
	expect(t).not.toContain('48687.50%')
	expect(t).not.toContain('4.87%')
})

it('算不出来的溢价渲染「—」, A/H 恰好同价渲染 0.00% —— 两者不得混淆', async () => {
	h.impl = async () =>
		payload([
			// 缺 A 侧收盘价(东电退已退市, 日K分区里没有)
			row({
				a_symbol: '000585.SZ', a_name: '东电退', a_close: null, a_price_date: null,
				h_symbol: '00042.HK', h_name: '东北电气', h_close: 0.182, premium_pct: null,
			}),
			// 恰好同价: 0.00% 是正确答案
			row({
				a_symbol: '600000.SH', a_name: '浦发银行', a_close: 10,
				h_symbol: '00001.HK', h_name: '同价样本', h_close: 10 / FX_RATE, premium_pct: 0,
			}),
		], {missing_price_count: 1})
	await mount()
	await settled('000585.SZ')
	const t = text()
	expect(t).toContain('0.00%'), '同价必须显示 0.00%, 而不是「—」'
	const broken = bodyRows().find(tr => tr.textContent?.includes('000585.SZ'))!
	const ok = bodyRows().find(tr => tr.textContent?.includes('600000.SH'))!
	expect(cells(broken)[8]).toBe('—'), '算不出来必须是「—」'
	expect(cells(broken)[2]).toBe('—'), '缺的那侧收盘价也必须是「—」'
	expect(cells(ok)[8]).toBe('0.00%')
	expect(cells(ok)[8]).not.toBe('—')
})

it('负溢价(H 股更贵)带负号并用跌色, 与正溢价可区分', async () => {
	// 真实尾部: 300750.SZ 宁德时代 A=227.78 / 03750.HK H=335.0 ⇒ 约 -20.19%
	const p = premiumOf(227.78, 335.0)
	expect(p).toBeCloseTo(-20.192, 2)
	h.impl = async () =>
		payload([
			row({a_symbol: '300750.SZ', a_name: '宁德时代', a_close: 227.78, h_symbol: '03750.HK', h_close: 335.0, premium_pct: p}),
		])
	await mount()
	await settled('300750.SZ')
	const td = bodyRows()[0].querySelectorAll('td')[8]
	expect(td.textContent).toBe('-20.19%')
	expect(td.className).toContain('text-bear'), 'H 股更贵用绿(跌色)'
})

// ── 数据质量计数 ──

it('质量计数为 0 时不打扰, 大于 0 时必须可见', async () => {
	h.impl = async () => payload([row({})])
	await mount()
	await settled('601398.SH')
	expect(qualityFlags()).toEqual([]), '四个计数全为 0 时不渲染任何徽标'

	h.impl = async () =>
		payload(
			[
				row({a_symbol: '000585.SZ', a_close: null, a_price_date: null, premium_pct: null}),
				row({a_symbol: '600000.SH', h_price_date: '2026-11-13', premium_pct: null}),
			],
			{missing_price_count: 1, stale_price_count: 1, price_lag_count: 1},
		)
	await remount()
	await settled('缺收盘价')
	const flags = qualityFlags().join('|')
	expect(flags).toContain('缺收盘价')
	expect(flags).toContain('价格过期')
	expect(flags).toContain('日期错位')
	// 🔴 计数必须直接印在徽标上, 不能只藏在 title 里
	expect(flags).toMatch(/缺收盘价1/)
})

it('歧义丢弃点名到具体 A 股代码, 不只给一个数字', async () => {
	// 后端刻意不猜: 一个 A 代码对多个 H 柜台且上市日缺失/并列 ⇒ 整组丢弃并上报
	h.impl = async () =>
		payload([row({})], {
			candidate_count: 3,
			ambiguous_a_symbols: ['600849.SH', '601607.SH'],
		})
	await mount()
	await settled('歧义丢弃')
	const t = text()
	expect(t).toContain('歧义丢弃')
	expect(t).toContain('600849.SH')
	expect(t).toContain('601607.SH')
	// 候选 3 / 产出 1 ⇒ 差值必须能被四个计数解释, 页面上两个数都要在
	expect(t).toContain('候选 3')
})

it('A/H 价格日期不是同一天时, 行内把 H 侧日期标成警告色', async () => {
	h.impl = async () =>
		payload(
			[row({a_price_date: '2026-11-20', h_price_date: '2026-11-13', premium_pct: null})],
			{price_lag_count: 1},
		)
	await mount()
	await settled('601398.SH')
	const td = bodyRows()[0].querySelectorAll('td')[7]
	expect(td.textContent).toBe('2026-11-13')
	expect(td.className).toContain('text-warning')
	expect(td.textContent).not.toBe('—'), '日期是事实, 照实显示; 不可比的是溢价率'
})

// ── 币种不得混排 ──

it('H 收盘标注 HKD, A 收盘不标 —— 两种币种不得看起来可直接相除', async () => {
	h.impl = async () => payload([row({a_close: 7.28, h_close: 6.79})])
	await mount()
	await settled('601398.SH')
	const c = cells(bodyRows()[0])
	expect(c[2]).toBe('7.28'), 'A 收盘是 CNY, 不加币种后缀(页面口径已声明)'
	expect(c[6]).toContain('6.790')
	expect(c[6]).toContain('HKD'), 'H 收盘必须标 HKD, 否则 7.28 与 6.790 看着像同币种可直接比'
})

it('H 收盘的 title 给出折人民币后的值与所用汇率, 便于手工复算', async () => {
	h.impl = async () => payload([row({a_close: 7.28, h_close: 6.79})])
	await mount()
	await settled('601398.SH')
	const td = bodyRows()[0].querySelectorAll('td')[6]
	const title = td.getAttribute('title') ?? ''
	expect(title).toContain((6.79 * FX_RATE).toFixed(3))
	expect(title).toContain(FX_RATE.toFixed(6))
})

// ── 排序 / 筛选 / 统计 ──

it('按溢价率降序时 null 沉底, 不占据首屏', async () => {
	// 后端 polars 的 nulls_last 只管它自己那次排序; 前端翻转方向时会再犯一次同坑
	h.impl = async () =>
		payload([
			row({a_symbol: '000585.SZ', a_close: null, a_price_date: null, premium_pct: null}),
			row({a_symbol: '000488.SZ', a_close: 1.6, h_symbol: '01812.HK', h_close: 0.32, premium_pct: premiumOf(1.6, 0.32)}),
			row({a_symbol: '300750.SZ', a_close: 227.78, h_symbol: '03750.HK', h_close: 335.0, premium_pct: premiumOf(227.78, 335.0)}),
		], {missing_price_count: 1})
	await mount()
	await settled('000488.SZ')
	// 默认就是溢价率降序
	expect(bodyRows().map(tr => cells(tr)[0])).toEqual(['000488.SZ', '300750.SZ', '000585.SZ'])

	// 点一次表头翻成升序 —— null 仍必须沉底
	const header = [...host!.querySelectorAll<HTMLButtonElement>('th button')].find(b =>
		b.textContent?.includes('溢价率'),
	)!
	await act(async () => {
		header.click()
	})
	expect(bodyRows().map(tr => cells(tr)[0])).toEqual(['300750.SZ', '000488.SZ', '000585.SZ'])
})

it('筛选「A 股溢价 / H 股更贵」只留对应符号的行, 算不出来的两边都不算', async () => {
	h.impl = async () =>
		payload([
			row({a_symbol: '000488.SZ', a_close: 1.6, h_symbol: '01812.HK', h_close: 0.32, premium_pct: premiumOf(1.6, 0.32)}),
			row({a_symbol: '300750.SZ', a_close: 227.78, h_symbol: '03750.HK', h_close: 335.0, premium_pct: premiumOf(227.78, 335.0)}),
			row({a_symbol: '000585.SZ', a_close: null, a_price_date: null, premium_pct: null}),
		], {missing_price_count: 1})
	await mount()
	await settled('000488.SZ')

	const click = async (label: string) => {
		const btn = [...host!.querySelectorAll<HTMLButtonElement>('button')].find(b => b.textContent === label)!
		await act(async () => {
			btn.click()
		})
	}
	await click('A 股溢价')
	expect(bodyRows().map(tr => cells(tr)[0])).toEqual(['000488.SZ'])
	await click('H 股更贵')
	expect(bodyRows().map(tr => cells(tr)[0])).toEqual(['300750.SZ'])
	await click('全部')
	expect(bodyRows()).toHaveLength(3)
})

it('概览统计: 中位溢价 / A 股更贵家数 / H 股更贵家数 / 极值', async () => {
	const hi = premiumOf(1.6, 0.32) // ≈ +486.875
	const mid = premiumOf(12.81, 3.085) // ≈ +387.382
	const lo = premiumOf(227.78, 335.0) // ≈ -20.192
	h.impl = async () =>
		payload([
			row({a_symbol: '000488.SZ', a_close: 1.6, h_symbol: '01812.HK', h_close: 0.32, premium_pct: hi}),
			row({a_symbol: '002703.SZ', a_close: 12.81, h_symbol: '01057.HK', h_close: 3.085, premium_pct: mid}),
			row({a_symbol: '300750.SZ', a_close: 227.78, h_symbol: '03750.HK', h_close: 335.0, premium_pct: lo}),
			row({a_symbol: '000585.SZ', a_close: null, a_price_date: null, premium_pct: null}),
		], {missing_price_count: 1})
	await mount()
	await settled('中位溢价')
	const t = text()
	expect(t).toContain('配对')
	// 中位数只统计算得出来的 3 个 ⇒ 排序后 [-20.19, 387.38, 486.88] 取中间
	expect(t).toContain('+387.38%')
	expect(t).toContain('+486.88%')
	expect(t).toContain('-20.19%')
	// 🔴 「已算出 3」与「配对 4」必须同时可见, 否则中位数看着像基于全量
	expect(t).toContain('已算出')
	expect(t).toMatch(/已算出3|已算出\s*3/)
})

it('搜索按 A/H 代码与名称过滤', async () => {
	h.impl = async () =>
		payload([
			row({a_symbol: '601398.SH', a_name: '工商银行', h_symbol: '01398.HK', h_name: '工商银行'}),
			row({a_symbol: '601899.SH', a_name: '紫金矿业', h_symbol: '02899.HK', h_name: '紫金矿业'}),
		])
	await mount()
	await settled('601398.SH')
	expect(bodyRows()).toHaveLength(2)

	const input = host!.querySelector<HTMLInputElement>('input[placeholder="A/H 代码或名称"]')!
	const type = async (v: string) => {
		await act(async () => {
			const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
			setter.call(input, v)
			input.dispatchEvent(new Event('input', {bubbles: true}))
		})
	}
	await type('02899')
	expect(bodyRows().map(tr => cells(tr)[0])).toEqual(['601899.SH'])
	await type('工商')
	expect(bodyRows().map(tr => cells(tr)[0])).toEqual(['601398.SH'])
	await type('不存在的关键词')
	// 🔴 空态那一行是 tbody 里的 colSpan 占位 <tr>, 不能拿 bodyRows().length 当数据行数
	expect(dataRows()).toHaveLength(0)
	expect(text()).toContain('当前过滤条件下没有匹配的行')
})

// ── 空态与缓存 key ──

it('配对表返回空时给出空态(与故障可区分)', async () => {
	h.impl = async () => payload([], {candidate_count: 0})
	await mount()
	await settled('上游配对表返回空')
	const t = text()
	expect(t).toContain('上游配对表返回空')
	expect(t).not.toContain('AH 溢价数据源不可用')
	// 🔴 空响应也必须带汇率横幅: 汇率是端点契约的一部分, 不随 items 为空而消失
	expect(t).toContain(FX_RATE.toFixed(6))
})

it('查询 key 是静态的, 且挂在港股本地数据失效集合里', () => {
	// 端点无参数 ⇒ key 不含窗口; 但 H 侧价格取自 kline_hk_daily.close、
	// 消歧用的上市日与名称取自 instruments_hk ⇒ 港股同步完必须失效
	expect(QK.hkAhPremium).toEqual(['hk-ah-premium'])
	expect(HK_DATA_QUERY_PREFIXES.has(QK.hkAhPremium[0])).toBe(true)
})
