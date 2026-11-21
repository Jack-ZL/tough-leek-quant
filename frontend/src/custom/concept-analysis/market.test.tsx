// @vitest-environment jsdom
// 概念板块页的市场门控 (方案 §3.4 / §11「前端隐藏 + 明示『港股无概念板块数据源』」)
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ConceptAnalysis } from './Page'
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
// 详情弹窗 / RPS 对话框与本页的市场门控无关, 且各自拖一整条查询链, 直接替空。
vi.mock('@/components/StockPreviewDialog', () => ({ StockPreviewDialog: () => null }))
vi.mock('@/components/RpsRotationDialog', () => ({ RpsRotationDialog: () => null }))

// 一份**真实的 A 股概念数据**: 门控失效时它就会被渲染出来, 断言才抓得住串市场。
const CN_CONCEPT = '猪肉概念'
const CN_SYMBOL = '000001.SZ'
const FIELDS = [
  { name: 'symbol', dtype: 'string', label: '股票代码' },
  { name: 'name', dtype: 'string', label: '股票简称' },
  { name: 'concept', dtype: 'string', label: '概念' },
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
      id: 'ext_gn_ths', label: '同花顺概念', mode: 'snapshot', fields: FIELDS,
      created_at: '2026-01-01', updated_at: '2026-01-01',
    }],
  } as Awaited<ReturnType<typeof api.extDataList>>)
  vi.mocked(api.extDataRows).mockResolvedValue({
    id: 'ext_gn_ths', label: '同花顺概念', mode: 'snapshot', date: '2026-11-13',
    total: 1, limit: 12000, fields: FIELDS,
    rows: [{ symbol: CN_SYMBOL, name: '平安银行', concept: CN_CONCEPT }],
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
    <QueryClientProvider client={client}><ConceptAnalysis /></QueryClientProvider>,
  ))
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

const text = () => host.textContent ?? ''

describe('A 股: 既有行为不变', () => {
  it('正常渲染概念数据, 不出现无源文案', async () => {
    await mount()
    expect(text()).toContain(CN_CONCEPT)
    expect(text()).not.toContain(boardNoSourceCopy('concept', 'HK')!.title)
    expect(api.extDataRows).toHaveBeenCalled()
  })
})

describe('港股: 前端隐藏 + 明示无源', () => {
  beforeEach(async () => {
    setMarket('HK')
  })

  it('渲染「港股无概念板块数据源」空态', async () => {
    await mount()
    const copy = boardNoSourceCopy('concept', 'HK')!
    expect(text()).toContain(copy.title)
    expect(text()).toContain('东财板块中心')
  })

  it('🔴 绝不渲染 A 股概念数据 (不用假数据冒充)', async () => {
    await mount()
    expect(text()).not.toContain(CN_CONCEPT)
    expect(text()).not.toContain(CN_SYMBOL)
    expect(text()).not.toContain('平安银行')
  })

  it('🔴 港股态连请求都不发 (门控在查询层, 不只停在渲染层)', async () => {
    await mount()
    expect(api.extDataList).not.toHaveBeenCalled()
    expect(api.extDataRows).not.toHaveBeenCalled()
    expect(api.marketSnapshot).not.toHaveBeenCalled()
  })

  it('不渲染「暂无概念数据」这类会被读成「同步一下就有」的引导', async () => {
    await mount()
    expect(text()).not.toContain('暂无概念数据')
    expect(text()).not.toContain('从同花顺获取概念分类数据后即可使用概念板块')
  })

  it('🔴 端到端: 行业对照句真的落到 DOM, 被删掉的错话不许回潮', async () => {
    await mount()
    // 「表里有」≠「页面上有」: 这条钉的是**渲染结果**, 防止将来给 EmptyState 传了 title 漏传 hint。
    // 用户往往刚从行业板块页(港股态有恒生行业数据)切过来, 少了这句会读成自相矛盾。
    expect(text()).toContain('港股的行业分类不在此列')
    expect(text()).toContain('恒生行业')
    // PR6-1 之前文案写着「东财网页版同样没有港股板块分类」—— 港股行业分类恰恰来自东财 f100,
    // 那句话与行业页的真实数据直接冲突, 已删。「无需改代码」同理(见 board-market-support.test.ts)。
    expect(text()).not.toContain('东财网页版')
    expect(text()).not.toContain('无需改代码')
  })

  it('🔴 端到端: 港股态整页零可交互元素 (没有按钮能点出一次 A 股请求)', async () => {
    await mount()
    // 提前 return 之后不该留下任何 A 股态入口(配置数据源 / 拉取 / RPS 轮动 / 排序 / 搜索)。
    // 只要还剩一个按钮, 用户就能点出一次白拉, 甚至一次 A 股数据泄漏 —— 比文案写错严重得多。
    expect(host.querySelectorAll('button, a, input, select, [role="button"]').length).toBe(0)
    // 整页只剩 PageHeader + EmptyState 两块。
    expect(host.querySelector('h1')?.textContent).toBe('概念板块')
    expect(host.querySelector('h2')?.textContent).toBe(boardNoSourceCopy('concept', 'HK')!.title)
  })

  it('切回 A 股后恢复正常渲染 (门控可逆, 不是一次性降级)', async () => {
    await mount()
    expect(text()).toContain(boardNoSourceCopy('concept', 'HK')!.title)
    await act(async () => setMarket(DEFAULT_MARKET))
    for (let i = 0; i < 5; i++) {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    }
    expect(text()).toContain(CN_CONCEPT)
    expect(text()).not.toContain(boardNoSourceCopy('concept', 'HK')!.title)
  })
})
