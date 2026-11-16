// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_MARKET } from '@/lib/market'
import { getMarket, setMarket } from '@/lib/marketStore'
import { Indices } from './Indices'

const mocks = vi.hoisted(() => ({
  indexQuotes: vi.fn(),
  indexDaily: vi.fn(),
  indexMinute: vi.fn(),
  syncIndexDaily: vi.fn(),
  hkIndices: vi.fn(),
  hkIndexDaily: vi.fn(),
  hkSyncIndexDaily: vi.fn(),
}))

vi.mock('@/lib/api', () => ({ api: mocks }))
vi.mock('@/lib/useSharedQueries', () => ({
  useCapabilities: () => ({ data: { capabilities: { 'kline.minute.batch': true } } }),
}))
vi.mock('@/components/charts/LightweightDaily', () => ({
  LightweightDaily: ({ data }: { data: { date: string }[] }) => (
    <div data-daily>{data.map(r => r.date).join(',')}</div>
  ),
}))
vi.mock('@/components/charts/LightweightIntraday', () => ({
  LightweightIntraday: () => <div data-intraday />,
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  // 🔴 全局市场 store 是**模块级**状态, 会跨用例残留: 上一个用例把市场切到港股后,
  // 下一个用例渲染 `/indices`(不带 market 参数) 会跟随 store 变成港股, 于是
  // 「缺省 = A 股」和「跨市场 symbol 不兜底」两组断言全部错位。这里显式复位。
  localStorage.removeItem('tf-market')
  setMarket(DEFAULT_MARKET)
  mocks.indexQuotes.mockResolvedValue({
    rows: [{ symbol: '000001.SH', name: '上证指数', last_price: 3900.12, change_pct: 0.36 }],
    count: 1,
  })
  mocks.indexDaily.mockImplementation(async (symbol: string) => ({
    symbol,
    name: '上证指数',
    source: 'parquet',
    rows: [{ date: '2026-11-12', open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 }],
  }))
  mocks.indexMinute.mockImplementation(async (symbol: string, date?: string) => ({
    symbol, name: null, date: date ?? null, rows: [],
  }))
  mocks.hkIndices.mockResolvedValue({
    rows: [{ symbol: 'HSI', name: '恒生指数', last_price: 23637.6, change_pct: -0.42 }],
    count: 4, source: 'realtime', market: 'HK', quote_delay_minutes: 15, currency: 'HKD',
  })
  mocks.hkIndexDaily.mockImplementation(async (symbol: string) => ({
    symbol,
    name: '恒生指数',
    volume_unit: null,
    currency: 'HKD',
    quote_delay_minutes: 15,
    // 🔴 端点是 ORDER BY date DESC, 页面必须看到升序
    rows: ['2026-11-13', '2026-11-12'].map(date => ({
      symbol, date, open: 1, high: 2, low: 0.5, close: 1.5, volume: 100,
    })),
  }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity, gcTime: Infinity,
  } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function renderAt(route: string) {
  await act(async () => root.render(
    <MemoryRouter initialEntries={[route]}>
      <QueryClientProvider client={client}><Indices /></QueryClientProvider>
    </MemoryRouter>,
  ))
  // React Query batches observer notifications on the next timer tick.
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

const text = () => host.textContent ?? ''
const tab = (label: string) =>
  host.querySelector<HTMLButtonElement>(`[role="tab"][aria-selected="true"]`)?.textContent === label
const delayBadge = () => host.querySelector<HTMLElement>('[aria-label^="延时"]')

describe('缺省 = A 股, 既有行为逐字不变', () => {
  it('渲染 A 股核心四只, 无延时徽章', async () => {
    await renderAt('/indices')
    expect(tab('A股')).toBe(true)
    for (const name of ['上证指数', '深证成指', '创业板指', '科创综指']) expect(text()).toContain(name)
    expect(delayBadge()).toBeNull()
    expect(text()).toContain('实时缓存 1 只指数')
    expect(text()).toContain('日K来源 parquet')
    expect(text()).toContain('同步指数日K')
  })

  it('URL 不带 market 时走 A 股端点', async () => {
    await renderAt('/indices?symbol=399006.SZ')
    expect(mocks.indexDaily.mock.calls[0][0]).toBe('399006.SZ')
    expect(mocks.hkIndices).not.toHaveBeenCalled()
  })
})

describe('港股市场', () => {
  it('渲染港股核心四只 + 延时徽章 + 港股文案', async () => {
    await renderAt('/indices?market=HK')
    expect(tab('港股')).toBe(true)
    for (const name of ['恒生指数', '恒生科技指数', '恒生中国企业指数', '恒生香港中资企业指数']) {
      expect(text()).toContain(name)
    }
    // A 股指数不得出现在港股页(串市场事故)
    expect(text()).not.toContain('上证指数')
    expect(delayBadge()?.getAttribute('aria-label')).toBe('延时 15 分钟')
    expect(text()).toContain('同步港股指数日K')
    expect(text()).toContain('延时快照')
    expect(text()).toContain('港股指数无分时数据源')
  })

  it('延时分钟数来自接口, 改成 20 就显示 20(前端无写死)', async () => {
    mocks.hkIndices.mockResolvedValue({
      rows: [], count: 4, source: 'realtime', quote_delay_minutes: 20, currency: 'HKD',
    })
    await renderAt('/indices?market=HK')
    expect(delayBadge()?.getAttribute('aria-label')).toBe('延时 20 分钟')
  })

  it('走港股端点, 且日K按升序渲染(端点返回 DESC)', async () => {
    await renderAt('/indices?market=HK')
    expect(mocks.hkIndices).toHaveBeenCalledWith(['HSI', 'HSTECH', 'HSCEI', 'HSCCI'])
    expect(mocks.hkIndexDaily.mock.calls[0][0]).toBe('HSI')
    expect(mocks.indexDaily).not.toHaveBeenCalled()
    expect(host.querySelector('[data-daily]')?.textContent).toBe('2026-11-12,2026-11-13')
  })

  it('🔴 快照降级时如实标注, 不伪装成实时', async () => {
    mocks.hkIndices.mockResolvedValue({
      rows: [], count: 4, source: 'index_daily', quote_delay_minutes: 15, currency: 'HKD',
    })
    await renderAt('/indices?market=HK')
    expect(text()).toContain('降级')
    expect(text()).not.toContain('延时快照')
  })

  it('同步按钮走港股端点', async () => {
    mocks.hkSyncIndexDaily.mockResolvedValue({ status: 'ok', count: 4, failed: [] })
    await renderAt('/indices?market=HK')
    const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('同步港股指数日K'))
    expect(btn).toBeTruthy()
    await act(async () => btn!.click())
    expect(mocks.hkSyncIndexDaily).toHaveBeenCalledTimes(1)
    expect(mocks.syncIndexDaily).not.toHaveBeenCalled()
  })
})

describe('🔴 跨市场 symbol 不兜底(过滤类请求, 方案 §8 规则 6)', () => {
  it('?symbol=HSI 但没带 market → 回落上证指数, 绝不拿 HSI 查 A 股目录', async () => {
    await renderAt('/indices?symbol=HSI')
    expect(tab('A股')).toBe(true)
    expect(mocks.indexDaily.mock.calls[0][0]).toBe('000001.SH')
    for (const call of mocks.indexDaily.mock.calls) expect(call[0]).not.toBe('HSI')
  })

  it('?market=HK&symbol=000001.SH → 回落恒生指数', async () => {
    await renderAt('/indices?market=HK&symbol=000001.SH')
    expect(mocks.hkIndexDaily.mock.calls[0][0]).toBe('HSI')
    for (const call of mocks.hkIndexDaily.mock.calls) expect(call[0]).not.toBe('000001.SH')
  })
})

describe('市场切换', () => {
  it('点「港股」切市场并清掉 A 股标的', async () => {
    await renderAt('/indices?symbol=399006.SZ')
    expect(mocks.indexDaily.mock.calls[0][0]).toBe('399006.SZ')
    const hkTab = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(b => b.textContent === '港股')
    await act(async () => hkTab!.click())
    for (let i = 0; i < 5; i++) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    expect(tab('港股')).toBe(true)
    expect(text()).toContain('恒生指数')
    // 切市场后不得继续用 A 股标的查港股日K
    for (const call of mocks.hkIndexDaily.mock.calls) expect(call[0]).not.toBe('399006.SZ')
    expect(mocks.hkIndexDaily.mock.calls[0][0]).toBe('HSI')
  })

  it('点当前市场不重新请求', async () => {
    await renderAt('/indices')
    const before = mocks.indexDaily.mock.calls.length
    const cnTab = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(b => b.textContent === 'A股')
    await act(async () => cnTab!.click())
    expect(mocks.indexDaily.mock.calls.length).toBe(before)
  })
})

// ===== URL ↔ 全局市场 store 双向对齐 =====
// 侧栏切换器把市场写进 store, 指数页把市场写进 URL; 两边都能是入口, 所以必须
// 幂等地互相同步, 且**URL 优先**(可分享链接不能被本地偏好覆盖)。

function LocationProbe() {
  const loc = useLocation()
  return <div data-loc>{loc.search}</div>
}

async function renderWithProbe(route: string) {
  await act(async () => root.render(
    <MemoryRouter initialEntries={[route]}>
      <LocationProbe />
      <QueryClientProvider client={client}><Indices /></QueryClientProvider>
    </MemoryRouter>,
  ))
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

const search = () => host.querySelector<HTMLElement>('[data-loc]')?.textContent ?? ''
const clickTab = async (label: string) => {
  const btn = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(b => b.textContent === label)
  await act(async () => btn!.click())
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

describe('URL ↔ 全局市场 store 双向对齐', () => {
  it('带 market=HK 进来 → 写回全局 store (侧栏切换器跟着变港股)', async () => {
    await renderWithProbe('/indices?market=HK')
    expect(getMarket()).toBe('HK')
    expect(tab('港股')).toBe(true)
    // URL 已带参数, 不该再重复改写
    expect(search()).toBe('?market=HK')
  })

  it('store 是港股但 URL 没带 market → 补进 URL (链接可分享)', async () => {
    setMarket('HK')
    await renderWithProbe('/indices')
    expect(search()).toContain('market=HK')
    expect(tab('港股')).toBe(true)
  })

  it('🔴 URL 优先于 store: ?market=CN + store=HK → 停在 A 股并把 store 拉回', async () => {
    setMarket('HK')
    await renderWithProbe('/indices?market=CN')
    expect(tab('A股')).toBe(true)
    expect(getMarket()).toBe('CN')
    expect(text()).toContain('上证指数')
    expect(text()).not.toContain('恒生指数')
  })

  it('🔴 切回 A 股后不会被打回港股 (selectMarket 必须同时写 store)', async () => {
    // 回归: A 股的 indexPageParams 不带 market 参数, 若 selectMarket 漏写 store,
    // 对齐 effect 会立刻把 market=HK 塞回 URL, 页内切换器等于点不动。
    setMarket('HK')
    await renderWithProbe('/indices')
    expect(search()).toContain('market=HK')
    await clickTab('A股')
    expect(tab('A股')).toBe(true)
    expect(getMarket()).toBe('CN')
    expect(search()).not.toContain('market=HK')
    expect(text()).toContain('上证指数')
  })

  it('A 股态下 URL 保持干净 (缺省不写 market 参数)', async () => {
    await renderWithProbe('/indices')
    expect(search()).toBe('')
    expect(getMarket()).toBe('CN')
  })
})
