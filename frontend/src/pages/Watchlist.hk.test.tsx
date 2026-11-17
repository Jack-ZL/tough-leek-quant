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
