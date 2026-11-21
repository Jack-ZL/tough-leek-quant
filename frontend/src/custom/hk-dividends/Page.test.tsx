// @vitest-environment jsdom
// 港股分红日历的三条口径回归闸 (CONTRIBUTING §3.1 / §4):
//   1. dividend_yield_pct 已是**百分比数值**, 前端只拼 '%' —— 谁再乘 100 就错 100 倍;
//   2. null(算不出来) 与 0(确认纯送转) 必须渲染成不同的东西;
//   3. 上游 502 必须显示成**故障**, 不能落进「该窗口内没有除净事件」的空态。
// 三条都是「静默给出误导性数字」级别的缺陷, 肉眼 review 看不出来, 只能用测试钉住。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, type HkDividendCalendarResponse, type HkDividendEvent } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { HkDividends } from './Page'

const h = vi.hoisted(() => ({ impl: null as null | (() => Promise<unknown>) }))

vi.mock('@/lib/api', async importActual => {
  const actual = await importActual<typeof import('@/lib/api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      hkDividends: () => {
        if (!h.impl) throw new Error('测试未配置 hkDividends 桩')
        return h.impl()
      },
    },
  }
})

/** 与 Page 内部的缺省窗口算法一致: 今天 → 今天 + 60 天。 */
function defaultWindow(): { start: string; end: string } {
  const p = (n: number) => String(n).padStart(2, '0')
  const iso = (d: Date) => `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
  const today = new Date()
  const end = new Date()
  end.setDate(end.getDate() + 60)
  return { start: iso(today), end: iso(end) }
}

function event(over: Partial<HkDividendEvent>): HkDividendEvent {
  return {
    symbol: '00700.HK', name: '腾讯控股', industry: '软件服务',
    ex_date: '2026-11-25', pay_date: '2026-12-28', notice_date: '2026-08-28',
    fiscal_year: '2026', report_type: '中期分配', plan: '每股派港币1.0元',
    dividend_hkd: 1.0, bonus_ratio: 0.0, parsed: true,
    raw_close: 512.5, price_date: '2026-11-20', dividend_yield_pct: 0.1951,
    ...over,
  }
}

function payload(items: HkDividendEvent[], over: Partial<HkDividendCalendarResponse> = {}) {
  const w = defaultWindow()
  return {
    items, total: items.length, start: w.start, end: w.end,
    market: 'HK', currency: 'HKD', source: 'eastmoney_datacenter',
    unparsed_count: items.filter(i => !i.parsed).length,
    missing_close_count: items.filter(i => i.raw_close == null).length,
    ...over,
  } satisfies HkDividendCalendarResponse
}

let root: Root | null = null
let host: HTMLElement | null = null

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
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
    defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } },
  })
  root = createRoot(host!)
  await act(async () => {
    root!.render(
      <QueryClientProvider client={client}>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <HkDividends />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  return host!
}

/** react-query 的通知批处理需要多个微任务轮次, act 单次空 flush 不够 — 轮询等待。
 *  每轮先 act 冲一次定时器, 让状态更新落在 act 里, 避免满屏 act 警告。 */
async function settled(marker: string | RegExp): Promise<void> {
  await vi.waitFor(async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    expect(text()).toMatch(marker)
  }, { timeout: 3000 })
}

function text(): string {
  return host?.textContent ?? ''
}

it('把 dividend_yield_pct 当百分比数值渲染, 不再乘 100', async () => {
  // 真实抽样: 江波龙 09976.HK, 每股派港币 2.546 / 不复权收盘 112.4 ⇒ 2.265124(%)
  h.impl = async () => payload([event({
    symbol: '09976.HK', name: '江波龙', dividend_hkd: 2.546, raw_close: 112.4,
    dividend_yield_pct: 2.546 / 112.4 * 100,
  })])
  await mount()
  await settled('2.27%')
  // 🔴 乘了 100 会得到 226.51%; 当成小数直接拼会得到 0.02%
  expect(text()).not.toContain('226.51%')
  expect(text()).not.toContain('0.02%')
})

it('算不出来的股息率渲染「—」, 纯送转渲染 0.00% —— 两者不得混淆', async () => {
  h.impl = async () => payload([
    // 缺收盘价: 未同步/停牌/人民币柜台 ⇒ 股息率不可知
    event({
      symbol: '80992.HK', name: '联想集团-R', ex_date: '2026-12-09',
      raw_close: null, price_date: null, dividend_yield_pct: null,
    }),
    // 纯送转: 现金派息确认为 0 ⇒ 0.00% 是正确答案
    event({
      symbol: '06656.HK', name: '百应租赁', ex_date: '2026-12-17',
      plan: '每10股转20股', dividend_hkd: 0.0, bonus_ratio: 2.0,
      raw_close: 62.65, dividend_yield_pct: 0.0,
    }),
  ])
  await mount()
  await settled('80992.HK')
  const t = text()
  expect(t).toContain('0.00%'), '纯送转必须显示 0.00%, 而不是「—」'
  expect(t).toContain('—'), '缺收盘价必须显示「—」, 而不是 0.00%'
  // 缺收盘价那行的收盘列也必须是「—」
  const row = [...host!.querySelectorAll('tr')].find(tr => tr.textContent?.includes('80992.HK'))!
  expect(row.textContent).toContain('—')
  expect(row.textContent).not.toContain('0.00%')
})

it('未解析的方案仍整行保留, 展示原文并标注「未解析」', async () => {
  // 除净日是事实, 不能因为源数据没给港币折算值就把整条公司行动藏掉
  h.impl = async () => payload([event({
    symbol: '00005.HK', name: '汇丰控股', plan: '每股派美元0.1元(可选择以股代息)',
    dividend_hkd: null, bonus_ratio: null, parsed: false,
    raw_close: 48.0, dividend_yield_pct: null,
  })])
  await mount()
  await settled('00005.HK')
  const t = text()
  expect(t).toContain('00005.HK')
  expect(t).toContain('每股派美元0.1元(可选择以股代息)')
  expect(t).toContain('未解析')
  expect(t).toContain('方案未解析')
})

it('数据质量计数为 0 时不打扰, 大于 0 时必须可见', async () => {
  h.impl = async () => payload([event({})], { unparsed_count: 0, missing_close_count: 0 })
  await mount()
  await settled('00700.HK')
  expect(text()).not.toContain('方案未解析')
  expect(text()).not.toContain('缺收盘价')

  // 重新挂载一次带缺陷的响应
  root?.unmount()
  host?.remove()
  host = document.createElement('div')
  document.body.append(host)
  h.impl = async () => payload([
    event({ symbol: '00005.HK', parsed: false, dividend_hkd: null, dividend_yield_pct: null }),
    event({ symbol: '80992.HK', ex_date: '2026-12-09', raw_close: null, price_date: null, dividend_yield_pct: null }),
  ])
  await mount()
  await settled('方案未解析')
  expect(text()).toContain('方案未解析')
  expect(text()).toContain('缺收盘价')
})

it('上游 502 显示成数据源故障, 而不是「没有除净事件」', async () => {
  h.impl = async () => { throw new ApiError('港股分红数据源不可用: datacenter 报表失败', 502) }
  await mount()
  await settled('港股分红数据源不可用')
  const t = text()
  expect(t).toContain('港股分红数据源不可用')
  // 🔴 关键: 故障不得伪装成合法的空结果
  expect(t).not.toContain('该窗口内没有除净事件')
})

it('窗口内确实没有事件时给出空态(与故障可区分)', async () => {
  h.impl = async () => payload([])
  await mount()
  await settled('该窗口内没有除净事件')
  const t = text()
  expect(t).toContain('该窗口内没有除净事件')
  expect(t).not.toContain('港股分红数据源不可用')
})

it('按股息率降序时 null 沉底, 不占据首屏', async () => {
  // 后端 polars 的 sort(descending=True) 默认 nulls first; 前端若照抄同一直觉,
  // 「股息率从高到低」排出来第一屏会全是「—」, 看着像数据全坏了。
  h.impl = async () => payload([
    event({ symbol: '80992.HK', ex_date: '2026-12-09', raw_close: null, price_date: null, dividend_yield_pct: null }),
    event({ symbol: '08329.HK', ex_date: '2026-12-01', dividend_yield_pct: 40.23 }),
    event({ symbol: '09976.HK', ex_date: '2026-11-23', dividend_yield_pct: 2.27 }),
  ])
  await mount()
  await settled('08329.HK')
  const header = [...host!.querySelectorAll<HTMLButtonElement>('th button')]
    .find(b => b.textContent?.includes('股息率'))!
  await act(async () => { header.click() })
  const symbols = [...host!.querySelectorAll('tbody tr')]
    .map(tr => tr.querySelector('td:nth-child(2)')?.textContent)
    .filter(Boolean)
  expect(symbols).toEqual(['08329.HK', '09976.HK', '80992.HK'])
})

it('查询 key 带上窗口两端, 改日期不会被旧缓存顶掉', () => {
  // 数据不落库, 缓存就是唯一状态; key 漏了日期会让「换了窗口还是上一窗口的行」
  const a = QK.hkDividends('2026-11-20', '2026-12-31')
  const b = QK.hkDividends('2026-11-20', '2027-01-31')
  expect(a[0]).toBe('hk-dividends')
  expect(a).not.toEqual(b)
})
