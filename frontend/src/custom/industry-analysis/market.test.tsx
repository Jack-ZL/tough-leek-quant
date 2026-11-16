// @vitest-environment jsdom
// 行业板块页的市场门控 — 与 concept-analysis 同口径 (方案 §3.4: 东财 t:2 的 496 个
// 行业同样全是 A 股)。两个页面各自有一份 guard 代码, 必须各自钉住。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { IndustryAnalysis } from './Page'
import { api } from '@/lib/api'
import { DEFAULT_MARKET } from '@/lib/market'
import { boardNoSourceCopy } from '@/lib/board-market-support'
import { setMarket } from '@/lib/marketStore'

vi.mock('@/lib/api', () => ({ api: {
  extDataList: vi.fn(),
  extDataRows: vi.fn(),
  extDataPullRun: vi.fn(),
  marketSnapshot: vi.fn(),
} }))
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/RpsRotationDialog', () => ({ RpsRotationDialog: () => null }))
vi.mock('@/components/SectorRotationCard', () => ({ SectorRotationCard: () => null }))

// 一份**真实的 A 股行业数据**: 门控失效时它就会被渲染出来, 断言才抓得住串市场。
const CN_INDUSTRY = '银行'
const CN_SYMBOL = '000001.SZ'
const FIELDS = [
  { name: 'symbol', dtype: 'string', label: '股票代码' },
  { name: 'name', dtype: 'string', label: '股票简称' },
  { name: 'industry', dtype: 'string', label: '行业' },
]

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  localStorage.clear()
  setMarket(DEFAULT_MARKET)
  vi.mocked(api.extDataList).mockResolvedValue({
    items: [{
      id: 'ext_hy_ths', label: '同花顺行业', mode: 'snapshot', fields: FIELDS,
      created_at: '2026-01-01', updated_at: '2026-01-01',
    }],
  } as Awaited<ReturnType<typeof api.extDataList>>)
  vi.mocked(api.extDataRows).mockResolvedValue({
    id: 'ext_hy_ths', label: '同花顺行业', mode: 'snapshot', date: '2026-11-13',
    total: 1, limit: 12000, fields: FIELDS,
    rows: [{ symbol: CN_SYMBOL, name: '平安银行', industry: CN_INDUSTRY }],
  } as Awaited<ReturnType<typeof api.extDataRows>>)
  vi.mocked(api.marketSnapshot).mockResolvedValue({
    as_of: '2026-11-13',
    rows: [{ symbol: CN_SYMBOL, name: '平安银行', change_pct: 1.2 }],
  } as Awaited<ReturnType<typeof api.marketSnapshot>>)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function mount() {
  await act(async () => root.render(
    <QueryClientProvider client={client}><IndustryAnalysis /></QueryClientProvider>,
  ))
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

const text = () => host.textContent ?? ''
const hkCopy = boardNoSourceCopy('industry', 'HK')!

describe('A 股: 既有行为不变', () => {
  it('正常渲染行业数据, 不出现无源文案', async () => {
    await mount()
    expect(text()).toContain(CN_INDUSTRY)
    expect(text()).not.toContain(hkCopy.title)
    expect(api.extDataRows).toHaveBeenCalled()
  })
})

describe('港股: 前端隐藏 + 明示无源', () => {
  beforeEach(() => {
    setMarket('HK')
  })

  it('渲染「港股无行业板块数据源」空态', async () => {
    await mount()
    expect(text()).toContain(hkCopy.title)
    expect(text()).toContain('东财板块中心')
  })

  it('🔴 绝不渲染 A 股行业数据 (不用假数据冒充)', async () => {
    await mount()
    expect(text()).not.toContain(CN_INDUSTRY)
    expect(text()).not.toContain(CN_SYMBOL)
    expect(text()).not.toContain('平安银行')
  })

  it('🔴 港股态连请求都不发 (门控在查询层, 不只停在渲染层)', async () => {
    await mount()
    expect(api.extDataList).not.toHaveBeenCalled()
    expect(api.extDataRows).not.toHaveBeenCalled()
    expect(api.marketSnapshot).not.toHaveBeenCalled()
  })

  it('切回 A 股后恢复正常渲染 (门控可逆)', async () => {
    await mount()
    expect(text()).toContain(hkCopy.title)
    await act(async () => setMarket(DEFAULT_MARKET))
    for (let i = 0; i < 5; i++) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    expect(text()).toContain(CN_INDUSTRY)
    expect(text()).not.toContain(hkCopy.title)
  })
})
