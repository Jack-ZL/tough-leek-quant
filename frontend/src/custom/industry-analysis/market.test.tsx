// @vitest-environment jsdom
// 行业板块页的市场门控。
//
// 🔴 与 concept-analysis **不再同口径**: 港股行业**有源** (恒生行业随
// `/api/screener/market-snapshot?market=HK` 的 `industry` 列下发), 港股概念仍无源。
// 早先这里钉的是「港股态整页空态 + 连请求都不发」, 那是把「东财板块中心没有港股」
// 误当成「港股没有行业分类」; 现在钉的是**换通道不换契约**:
//   - 港股走快照通道, 不发扩展数据请求 (A 股行业表对港股毫无意义, 白拉);
//   - A 股走扩展数据通道, 行为逐字不变;
//   - 两个市场互不串数据 (最危险的症状是港股页渲染出 A 股行业)。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { IndustryAnalysis } from './Page'
import { api } from '@/lib/api'
import { DEFAULT_MARKET } from '@/lib/market'
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

// 一份**真实的港股快照**: 行业列来自 instruments_hk 的东财 f100 (恒生行业)。
const HK_INDUSTRY = '资讯科技器材'
const HK_SYMBOL = '00700.HK'
const HK_ROWS = [
  { symbol: HK_SYMBOL, name: '腾讯控股', industry: HK_INDUSTRY, close: 512.5, change_pct: 0.021, amount: 8e9, turnover_rate: 0.4, vol_ratio_5d: 1.2, market_cap: 4.7e12, float_market_cap: 4.7e12, consecutive_limit_ups: 0 },
  { symbol: '09988.HK', name: '阿里巴巴-W', industry: HK_INDUSTRY, close: 128.0, change_pct: -0.012, amount: 5e9, turnover_rate: 0.3, vol_ratio_5d: 0.9, market_cap: 2.4e12, float_market_cap: 2.4e12, consecutive_limit_ups: 0 },
  { symbol: '00005.HK', name: '汇丰控股', industry: '银行', close: 72.0, change_pct: 0.004, amount: 2e9, turnover_rate: 0.1, vol_ratio_5d: 1.0, market_cap: 1.3e12, float_market_cap: 1.3e12, consecutive_limit_ups: 0 },
]

let host: HTMLDivElement
let root: Root
let client: QueryClient

/** 按 market 返回各自的数据 —— 端点若不传 market, 港股态就会拿到 A 股行。 */
function mockSnapshotByMarket(hk: { as_of: string | null; rows: unknown[] }) {
  vi.mocked(api.marketSnapshot).mockImplementation((async (market: string) => (
    market === 'HK'
      ? hk
      : { as_of: '2026-11-13', rows: [{ symbol: CN_SYMBOL, name: '平安银行', change_pct: 1.2 }] }
  )) as typeof api.marketSnapshot)
}

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
  mockSnapshotByMarket({ as_of: '2026-11-20', rows: HK_ROWS })
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

describe('A 股: 既有行为不变 (扩展数据通道)', () => {
  it('正常渲染行业数据', async () => {
    await mount()
    expect(text()).toContain(CN_INDUSTRY)
    expect(text()).toContain('平安银行')
    expect(api.extDataRows).toHaveBeenCalled()
  })

  it('🔴 快照按 CN 取, 不渲染港股行业', async () => {
    await mount()
    expect(api.marketSnapshot).toHaveBeenCalledWith('CN')
    expect(text()).not.toContain(HK_INDUSTRY)
    expect(text()).not.toContain(HK_SYMBOL)
  })

  it('副标题走 N 级行业口径, 且带同花顺表的日期', async () => {
    await mount()
    expect(text()).toContain('级行业')
    expect(text()).not.toContain('恒生行业')
  })

  it('A 股专有入口都在 (RPS 轮动 / 配置数据源)', async () => {
    await mount()
    expect(text()).toContain('涨幅RPS轮动分析')
    expect(host.querySelector('[title="配置数据源"]')).not.toBeNull()
  })
})

describe('港股: 走快照通道读恒生行业', () => {
  beforeEach(() => {
    setMarket('HK')
  })

  it('🔴 快照必须带 market=HK (缺省会拿到 A 股行, 症状是港股页满屏 A 股代码)', async () => {
    await mount()
    expect(api.marketSnapshot).toHaveBeenCalledWith('HK')
  })

  it('渲染港股行业与港股标的', async () => {
    await mount()
    expect(text()).toContain(HK_INDUSTRY)
    expect(text()).toContain('腾讯控股')
    expect(text()).toContain('汇丰控股')
  })

  it('🔴 绝不渲染 A 股行业数据 (不用假数据冒充)', async () => {
    await mount()
    expect(text()).not.toContain(CN_SYMBOL)
    expect(text()).not.toContain('平安银行')
  })

  it('🔴 不发扩展数据请求 (港股没有同花顺行业表, 拉了也是白拉)', async () => {
    await mount()
    expect(api.extDataList).not.toHaveBeenCalled()
    expect(api.extDataRows).not.toHaveBeenCalled()
    expect(api.extDataPullRun).not.toHaveBeenCalled()
  })

  it('副标题标明恒生行业 + 港股自己的 as_of', async () => {
    await mount()
    expect(text()).toContain('恒生行业')
    expect(text()).toContain('2026-11-20')
    expect(text()).not.toContain('级行业')
  })

  it('🔴 轮动矩阵入口对港股开放 (后端已按 market 分派到恒生行业)', async () => {
    await mount()
    // /api/rps/rotation 现在按 market 分派, 港股走 rps_rotation._rotation_hk
    // (恒生行业 + 港股 enriched 日K), 是本地真数据。早先这里断言「隐藏」,
    // 是因为当时后端还没有分派、点开只会拿到一张 A 股矩阵 —— 那是串市场;
    // 现在再挡着就变成「有数据却说没有」。
    expect(text()).toContain('涨幅RPS轮动分析')
  })

  it('🔴 数据源配置入口仍然隐藏 (港股行业分类随快照下发, 没有扩展数据表可配)', async () => {
    await mount()
    expect(host.querySelector('[title="配置数据源"]')).toBeNull()
  })

  it('切回 A 股后恢复扩展数据通道 (门控可逆)', async () => {
    await mount()
    expect(text()).toContain(HK_INDUSTRY)
    await act(async () => setMarket(DEFAULT_MARKET))
    for (let i = 0; i < 5; i++) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    expect(text()).toContain(CN_INDUSTRY)
    expect(text()).toContain('平安银行')
    expect(api.extDataRows).toHaveBeenCalled()
  })
})

describe('港股空态: 区分「没同步」与「同步了但没分类」', () => {
  beforeEach(() => {
    setMarket('HK')
  })

  it('从没同步过 → 指路去同步, 不是「暂无数据」', async () => {
    mockSnapshotByMarket({ as_of: null, rows: [] })
    await mount()
    expect(text()).toContain('港股行情尚未同步')
    expect(text()).toContain('港股数据')
    // 「暂无数据」会被读成「刷新一下就有」, 用户会反复点刷新
    expect(text()).not.toContain('暂无数据')
  })

  it('有行情但 industry 整列为空 → 指路去同步维表', async () => {
    mockSnapshotByMarket({
      as_of: '2026-11-20',
      rows: HK_ROWS.map(({ industry: _industry, ...rest }) => rest),
    })
    await mount()
    expect(text()).toContain('港股行情缺少行业分类')
    expect(text()).toContain('同步维表')
  })

  it('🔴 分类为空的行不归进「未知」组 (会造出一个成分最多的假板块)', async () => {
    mockSnapshotByMarket({
      as_of: '2026-11-20',
      rows: [...HK_ROWS, { symbol: '01234.HK', name: '无行业公司', industry: null, change_pct: 0.01 }],
    })
    await mount()
    expect(text()).not.toContain('未知')
    expect(text()).not.toContain('无行业公司')
    expect(text()).toContain(HK_INDUSTRY)
  })
})
