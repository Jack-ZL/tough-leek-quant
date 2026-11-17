// @vitest-environment jsdom
/**
 * 自选页的港股延时角标。
 *
 * 🔴 钉住的是「分钟数只来自接口字段」这条契约: 港股快照是交易所授权的 15 分钟延时
 * 行情, 属正常状态。写死 15 会在源侧调整延时后静默说谎, 也会让 A 股凭空多出角标;
 * 复用 `QuoteFreshnessBadge` 则会把正常延时说成「数据源坏了」。
 */
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { DEFAULT_MARKET } from '@/lib/market'
import { setMarket } from '@/lib/marketStore'
import { Watchlist } from './Watchlist'

vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))
vi.mock('@/lib/watchlist-columns', async importOriginal => {
  const mod = await importOriginal<typeof import('@/lib/watchlist-columns')>()
  return { ...mod, loadColumnConfig: async () => mod.BUILTIN_COLUMNS
    .filter(c => ['builtin:symbol', 'builtin:candle', 'builtin:intraday'].includes(c.id))
    .map(c => ({ ...c, visible: true })) }
})
vi.mock('@/lib/useSharedQueries', () => ({
  usePreferences: () => ({ data: { minute_intraday_refresh: false } }),
  useCapabilities: () => ({ data: { capabilities: { 'kline.minute.batch': {} } } }),
  useQuoteStatus: () => ({ data: { running: false } }),
}))
vi.mock('@/components/stock-table/MiniCandlestick', () => ({
  MiniCandlestick: () => <output data-chart="daily" />,
}))
vi.mock('@/components/stock-table/MiniIntraday', () => ({
  MiniIntraday: () => <output data-chart="minute" />,
}))

const HK = '00700.HK'
const CN = '600000.SH'

const host = document.createElement('div')
let root: ReturnType<typeof createRoot>
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  localStorage.clear()
  // 🔴 marketStore 是模块级单例, 不清会在用例间泄漏市场态
  setMarket(DEFAULT_MARKET)
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

async function settle() {
  for (let i = 0; i < 5; i++) await act(async () => { await vi.advanceTimersByTimeAsync(20) })
}

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function mockRows(rows: Record<string, unknown>[]) {
  vi.stubGlobal('fetch', vi.fn(async (..._args: unknown[]) => new Response('{}')))
  vi.spyOn(api, 'watchlistList').mockResolvedValue({
    symbols: rows.map(r => ({ symbol: r.symbol as string, added_at: '2026-11-13' })),
  })
  vi.spyOn(api, 'watchlistGroups').mockResolvedValue({ groups: [] })
  vi.spyOn(api, 'watchlistEnriched').mockResolvedValue({
    rows, as_of: null, elapsed_ms: 0, dates: { stock: null, etf: null, index: null, hk: null },
  })
  vi.spyOn(api, 'klineDailyBatch').mockResolvedValue({ data: {} })
  vi.spyOn(api, 'klineMinuteBatch').mockResolvedValue({ data: {} })
}

async function render() {
  await act(async () => root.render(
    <MemoryRouter><QueryClientProvider client={client}><Watchlist /></QueryClientProvider></MemoryRouter>,
  ))
  await settle()
}

/** 某个标的所在行的延时角标文案 (无角标 → null) */
function delayBadgeOf(symbol: string): string | null {
  const row = Array.from(host.querySelectorAll('tbody tr'))
    .find(tr => tr.textContent?.includes(symbol))
  const badge = Array.from(row?.querySelectorAll('span[aria-label^="延时"]') ?? [])[0]
  return badge?.getAttribute('aria-label') ?? null
}

it('表格视图: 港股行标 15 分钟延时, A 股行不标', async () => {
  mockRows([
    { symbol: HK, name: '腾讯控股', asset_type: 'stock', close: 500, quote_delay_minutes: 15 },
    { symbol: CN, name: '浦发银行', asset_type: 'stock', close: 10, quote_delay_minutes: 0 },
  ])
  await render()

  expect(delayBadgeOf(HK)).toBe('延时 15 分钟')
  expect(delayBadgeOf(CN)).toBeNull()
})

it('卡片视图: 港股延时角标同样渲染', async () => {
  mockRows([
    { symbol: HK, name: '腾讯控股', asset_type: 'stock', close: 500, quote_delay_minutes: 15 },
  ])
  await render()
  const toggle = host.querySelector<HTMLButtonElement>('button[title="卡片视图"]')!
  await act(async () => toggle.click())
  await settle()

  expect(host.querySelector('span[aria-label="延时 15 分钟"]')).not.toBeNull()
})

it('延时分钟数来自接口而非写死: 后端给 0 就不渲染', async () => {
  mockRows([
    { symbol: HK, name: '腾讯控股', asset_type: 'stock', close: 500, quote_delay_minutes: 0 },
  ])
  await render()

  expect(host.querySelector('span[aria-label^="延时"]')).toBeNull()
})

it('旧后端不返回该字段时不渲染 (不得凭空补默认延时)', async () => {
  mockRows([{ symbol: HK, name: '腾讯控股', asset_type: 'stock', close: 500 }])
  await render()

  expect(host.querySelector('span[aria-label^="延时"]')).toBeNull()
})

// ===== 搜索框跟随全局市场 =====

function typeIntoSearch(text: string) {
  const input = host.querySelector<HTMLInputElement>('input[placeholder="搜索…"]')!
  return act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, text)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

function resultSymbols(): string[] {
  return [...host.querySelectorAll('button[aria-label^="快速加入自选"]')]
    .map(el => (el.getAttribute('aria-label') ?? '').replace('快速加入自选 ', ''))
}

it('搜索按当前市场传参: A 股态 CN, 港股态 HK', async () => {
  mockRows([])
  const search = vi.spyOn(api, 'instrumentSearch').mockResolvedValue({ results: [] })
  await render()
  await typeIntoSearch('007')
  await settle()
  expect(search.mock.calls[0][3]).toBe('CN')

  await act(async () => setMarket('HK'))
  await settle()
  await typeIntoSearch('0070')
  await settle()
  expect(search.mock.calls.at(-1)![3]).toBe('HK')
})

it('🔴 切市场不复用上一个市场的搜索结果(串市场)', async () => {
  // 同一个关键词在两个市场返回不同标的; queryKey 漏了 market 的话, 切到港股那一帧
  // react-query 会直接把 A 股缓存当港股结果渲染(staleTime 内连请求都不发)。
  vi.spyOn(api, 'instrumentSearch').mockImplementation(async (_q, _l, _t, market) => ({
    results: market === 'HK'
      ? [{ symbol: '00700.HK', name: '腾讯控股', code: '00700' }]
      : [{ symbol: '600000.SH', name: '浦发银行', code: '600000' }],
  }))
  mockRows([])
  await render()
  await typeIntoSearch('007')
  await settle()
  expect(resultSymbols()).toEqual(['600000.SH'])

  await act(async () => setMarket('HK'))
  await settle()
  expect(resultSymbols()).toEqual(['00700.HK'])
})
