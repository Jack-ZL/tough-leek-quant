// @vitest-environment jsdom
// 侧栏指数条 + 全局市场切换器 (方案 PR3 剩余: 侧栏指数条 market 感知 / 全局市场切换器)
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Layout } from './Layout'
import { api, type Preferences } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { DEFAULT_MARKET } from '@/lib/market'
import { coreIndexes } from '@/lib/marketContent'
import { MARKET_SWITCHER_SCOPE_HINT, MARKET_SWITCHER_SCOPE_HINT_CN, getMarket, setMarket } from '@/lib/marketStore'

const layoutMocks = vi.hoisted(() => ({ desktop: true, prefs: {} as Partial<Preferences> }))

vi.mock('@/lib/api', () => ({ api: {
  dataSources: vi.fn(async () => ({ builtin: [], custom: [], plugins: [] })),
  analysisMenus: vi.fn(async () => ({ items: [] })),
  indexQuotes: vi.fn(async () => ({ rows: [], count: 0 })),
  hkIndices: vi.fn(async () => ({ rows: [], count: 0, source: 'realtime', quote_delay_minutes: 15 })),
  watchlistGroups: vi.fn(async () => ({ groups: [] })),
  watchlistList: vi.fn(async () => ({ symbols: [] })),
  watchlistEnriched: vi.fn(async () => ({ rows: [] })),
  pipelineJobs: vi.fn(async () => ({ active_id: null, items: [] })),
  dataIntegrity: vi.fn(async () => ({ report: null, blocking_assets: [], data_revisions: {} })),
  alertsList: vi.fn(async () => ({ total: 0 })),
  quoteStatus: vi.fn(),
  miningRuns: vi.fn(async () => ({ items: [] })),
  intradayRefresh: vi.fn(async () => ({})),
} }))
vi.mock('@/lib/useSharedQueries', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/useSharedQueries')>(),
  useSettings: () => ({ data: { onboarding_completed: true } }),
  useCapabilityMatrix: () => ({ data: { capabilities: [] } }),
  usePreferences: () => ({ data: layoutMocks.prefs }),
  useVersion: () => ({ data: { version: '0.3.20' } }),
}))
vi.mock('@/lib/useQuoteStream', () => ({
  useQuoteStream: () => {}, useQuoteStreamStatus: () => 'connected',
}))
vi.mock('@/lib/updateCheck', () => ({ useUpdateCheck: () => ({ status: 'latest' }) }))
vi.mock('@/lib/useMediaQuery', () => ({ useIsDesktop: () => layoutMocks.desktop }))
// 🔴 /regime 已被上游迁进 custom/market-regime 扩展 (11 个页面从内置侧栏挪到 custom/),
// 内置 CORE_NAVIGATION 只剩 7 项。本用例的契约是「核心页不按市场过滤, 全部可达」, 所以
// mock 要把这条扩展导航补回来 —— 返回 [] 断言的是「扩展全未注册」的人造场景, 与真实
// 启动状态不符。字段形状见 lib/navigation.ts 的 getNavigationEntries。
vi.mock('@/extensions/registry', () => ({
  getFrontendExtensionNavigation: () => [{
    id: 'regime', routeId: 'regime', label: '市场环境', icon: () => null,
    route: { id: 'regime', path: '/regime', component: () => null },
    extensionId: 'official.market-regime',
  }],
}))
vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))
vi.mock('@/components/Toast', () => ({ ToastContainer: () => null, toast: vi.fn() }))
vi.mock('@/components/AlertToast', () => ({ AlertToastContainer: () => null }))
vi.mock('@/components/financials/AiAnalysisHost', () => ({ AiAnalysisHost: () => null }))
vi.mock('@/components/financials/AiReportBubble', () => ({ AiReportBubble: () => null }))
vi.mock('@/components/stock-analysis/StockAnalysisHost', () => ({ StockAnalysisHost: () => null }))
vi.mock('@/components/stock-analysis/StockAnalysisBubble', () => ({ StockAnalysisBubble: () => null }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

const CN_SYMBOLS = coreIndexes('CN').map(i => i.symbol)
const HK_SYMBOLS = coreIndexes('HK').map(i => i.symbol)

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  layoutMocks.desktop = true
  layoutMocks.prefs = { realtime_data_provider: 'fuyao', realtime_quotes_enabled: false }
  vi.mocked(api.quoteStatus).mockResolvedValue({ enabled: false, running: false, paused: false,
    is_trading_hours: false, mode: 'none', realtime_allowed: false } as Awaited<ReturnType<typeof api.quoteStatus>>)
  vi.mocked(api.indexQuotes).mockResolvedValue({
    rows: CN_SYMBOLS.map((symbol, i) => ({ symbol, name: `CN${i}`, last_price: 3900 + i, change_pct: 0.1 * i })),
    count: CN_SYMBOLS.length,
  } as Awaited<ReturnType<typeof api.indexQuotes>>)
  vi.mocked(api.hkIndices).mockResolvedValue({
    rows: HK_SYMBOLS.map((symbol, i) => ({ symbol, name: `HK${i}`, last_price: 23000 + i, change_pct: -0.1 * i })),
    count: HK_SYMBOLS.length, source: 'realtime', market: 'HK', quote_delay_minutes: 15, currency: 'HKD',
  } as Awaited<ReturnType<typeof api.hkIndices>>)
  localStorage.clear()
  localStorage.setItem('tf-nav-state', 'expanded')
  // 🔴 全局市场 store 是模块级状态, 跨用例残留会让「缺省 = A 股」的断言错位。
  setMarket(DEFAULT_MARKET)
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

async function mount(path = '/') {
  await act(async () => root.render(<MemoryRouter initialEntries={[path]}><QueryClientProvider client={client}>
    <Layout />
  </QueryClientProvider></MemoryRouter>))
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

const text = () => host.textContent ?? ''
const marketTab = (label: string) =>
  [...host.querySelectorAll<HTMLButtonElement>('[role="tablist"][aria-label="市场"] button')]
    .find(b => b.textContent === label)

describe('缺省 = A 股: 侧栏指数条行为逐字不变', () => {
  it('请求 A 股端点, 且只请求核心四只 (收窄口径未被 market 改造丢掉)', async () => {
    await mount()
    expect(api.indexQuotes).toHaveBeenCalledWith(CN_SYMBOLS)
    expect(api.hkIndices).not.toHaveBeenCalled()
  })

  it('queryKey 带 market, 且落在 A 股 key 前缀下', async () => {
    await mount()
    const q = client.getQueryCache().findAll({ queryKey: [...QK.indexQuotes, 'sidebar'] })
    expect(q.length).toBe(1)
    expect(q[0].queryKey).toEqual([...QK.indexQuotes, 'sidebar', 'CN'])
  })

  it('指数条链接不带 market 参数 (保持既有 A 股 URL 形态)', async () => {
    await mount()
    const href = host.querySelector<HTMLAnchorElement>('a[href^="/indices?symbol=000001.SH"]')
    expect(href).not.toBeNull()
    expect(href!.getAttribute('href')).not.toContain('market=')
  })

  it('A 股态渲染 A 股专属作用范围说明 (不复用港股提醒措辞)', async () => {
    await mount()
    expect(text()).toContain(MARKET_SWITCHER_SCOPE_HINT_CN)
    expect(text()).not.toContain(MARKET_SWITCHER_SCOPE_HINT)
  })
})

describe('切到港股: 指数条整体换源', () => {
  it('点侧栏「港股」后改请求港股端点, 不再碰 A 股端点', async () => {
    await mount()
    vi.mocked(api.indexQuotes).mockClear()
    await act(async () => marketTab('港股')!.click())
    for (let i = 0; i < 5; i++) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    expect(getMarket()).toBe('HK')
    expect(api.hkIndices).toHaveBeenCalledWith(HK_SYMBOLS)
    expect(api.indexQuotes).not.toHaveBeenCalled()
  })

  it('🔴 缓存不串市场: 两个市场各占一条 cache entry', async () => {
    await mount()
    await act(async () => marketTab('港股')!.click())
    for (let i = 0; i < 5; i++) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    const cn = client.getQueryCache().find({ queryKey: [...QK.indexQuotes, 'sidebar', 'CN'] })
    const hk = client.getQueryCache().find({
      queryKey: [...QK.hkIndexQuotes(HK_SYMBOLS.join(',')), 'sidebar', 'HK'],
    })
    expect(cn).not.toBeUndefined()
    expect(hk).not.toBeUndefined()
    expect(cn!.state.data).not.toBe(hk!.state.data)
    // A 股那条里绝不能出现港股行 (反之亦然)
    const cnRows = (cn!.state.data as { rows: { symbol: string }[] }).rows
    const hkRows = (hk!.state.data as { rows: { symbol: string }[] }).rows
    expect(cnRows.every(r => CN_SYMBOLS.includes(r.symbol))).toBe(true)
    expect(hkRows.every(r => HK_SYMBOLS.includes(r.symbol))).toBe(true)
  })

  it('指数条链接带上 market=HK (否则点进去会拿 HSI 查 A 股接口, 静默返回空)', async () => {
    await mount()
    await act(async () => marketTab('港股')!.click())
    for (let i = 0; i < 5; i++) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    const hrefs = [...host.querySelectorAll<HTMLAnchorElement>('a[href^="/indices?"]')]
      .map(a => a.getAttribute('href')!)
    expect(hrefs.length).toBeGreaterThan(0)
    for (const href of hrefs) {
      expect(href).toContain('market=HK')
      expect(href).toMatch(/symbol=(HSI|HSTECH|HSCEI|HSCCI)/)
    }
  })

  it('🔴 渲染作用范围说明, 防止用户以为整站都切了港股', async () => {
    await mount()
    await act(async () => marketTab('港股')!.click())
    for (let i = 0; i < 5; i++) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    expect(text()).toContain(MARKET_SWITCHER_SCOPE_HINT)
  })

  it('🔴 导航条目**不按市场过滤** (收窄方案的显式契约: 8 个核心页仍全部可达)', async () => {
    await mount()
    const before = ['/watchlist', '/screener', '/factors', '/backtest', '/regime', '/indices', '/data']
      .map(p => host.querySelector(`a[href="${p}"]`))
    expect(before.every(Boolean)).toBe(true)
    await act(async () => marketTab('港股')!.click())
    for (let i = 0; i < 5; i++) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    // 这些页面只有 A 股链路, 但**隐藏它们会让侧栏几乎空掉**且与用户的 nav 自定义
    // 配置打架; 约定改由切换器上的作用范围说明来兜住「不误导」。
    const after = ['/watchlist', '/screener', '/factors', '/backtest', '/regime', '/indices', '/data']
      .map(p => host.querySelector(`a[href="${p}"]`))
    expect(after.every(Boolean)).toBe(true)
  })
})

describe('切换器自身', () => {
  it('两个市场都在, 当前市场 aria-selected=true', async () => {
    await mount()
    const tabs = [...host.querySelectorAll<HTMLButtonElement>('[role="tablist"][aria-label="市场"] button')]
    expect(tabs.map(t => t.textContent)).toEqual(['A股', '港股'])
    expect(tabs[0].getAttribute('aria-selected')).toBe('true')
    expect(tabs[1].getAttribute('aria-selected')).toBe('false')
    await act(async () => tabs[1].click())
    expect(tabs[1].getAttribute('aria-selected')).toBe('true')
  })

  it('点当前市场不重新请求', async () => {
    await mount()
    vi.mocked(api.indexQuotes).mockClear()
    await act(async () => marketTab('A股')!.click())
    for (let i = 0; i < 3; i++) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    expect(api.indexQuotes).not.toHaveBeenCalled()
  })

  it('市场选择持久化到 localStorage', async () => {
    await mount()
    await act(async () => marketTab('港股')!.click())
    expect(localStorage.getItem('tf-market')).toBe('HK')
  })
})
