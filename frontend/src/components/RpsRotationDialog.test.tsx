// @vitest-environment jsdom
/**
 * RpsRotationDialog 的市场分派测试。
 *
 * 钉的是「控件与文案按市场取表 (lib/board-market-support.ts 的 ROTATION_SUPPORT)」
 * 这条契约, 而不是矩阵渲染细节 —— 后者由后端 tests/test_rps_rotation_hk.py 覆盖。
 *
 * 🔴 这里最有价值的两条断言是**否定式**的:
 *   港股态不渲染 1/2/3 级选择器 (恒生行业单层, 后端忽略 level);
 *   港股态不渲染「生成分析」按钮 (无大盘背景源, 后端会 fail-closed)。
 * 两个控件留着都不会报错, 只会让用户点了没反应 / 拿到一条 error 事件,
 * 属于「静默退化」, 靠肉眼 review 很难每次都拦住。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RpsRotationDialog } from './RpsRotationDialog'
import { ROTATION_SUPPORT } from '@/lib/board-market-support'
import type { RpsRotationData } from '@/lib/api'
import type { Market } from '@/lib/market'

const apiMock = vi.hoisted(() => ({
  rpsRotation: vi.fn(),
  rotationAnalyzeStream: vi.fn(),
}))
vi.mock('@/lib/api', () => ({
  api: apiMock,
  friendlyStreamError: (s: string) => s,
}))
// Markdown 渲染器依赖 marked + DOMPurify, 与本测试无关, 换成透传避免拖慢收集
vi.mock('@/components/financials/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => <div>{content}</div>,
}))
vi.mock('framer-motion', () => ({
  motion: {
    div: ({ children, ...rest }: Record<string, unknown>) => <div {...stripMotion(rest)}>{children as never}</div>,
  },
  AnimatePresence: ({ children }: { children: unknown }) => <>{children as never}</>,
}))

// framer-motion 的动画 props 传给真实 DOM 会报 unknown-prop 警告, 测试里直接丢掉
function stripMotion(props: Record<string, unknown>) {
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(props)) {
    if (['initial', 'animate', 'exit', 'transition', 'variants', 'layout', 'layoutId', 'drag', 'whileHover', 'whileTap', 'custom'].includes(k)) continue
    out[k] = v
  }
  return out
}

const DATES = ['2026-11-19', '2026-11-20']

function payload(dimLabel: string) {
  return {
    dates: DATES,
    columns: {
      '2026-11-19': [[`${dimLabel}A`, 0.012], [`${dimLabel}B`, -0.004]] as [string, number][],
      '2026-11-20': [[`${dimLabel}B`, 0.021], [`${dimLabel}A`, 0.003]] as [string, number][],
    },
    concept_count: 2,
  }
}

const EMPTY: RpsRotationData = { dates: [], columns: {}, concept_count: 0 }

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  apiMock.rpsRotation.mockReset()
  apiMock.rotationAnalyzeStream.mockReset()
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

async function mount(market: Market, kind: 'concept' | 'industry' = 'industry', data: RpsRotationData = payload('行业')) {
  apiMock.rpsRotation.mockResolvedValue(data)
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <RpsRotationDialog onClose={() => {}} kind={kind} market={market} />
      </QueryClientProvider>,
    )
  })
  // useQuery 的 promise 落地后再冲几轮宏任务, 让矩阵/空态真正渲染出来
  // (只 flush 微任务不够: React Query 的 setState 走调度器, isLoading 会卡在 true)
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

const text = () => host.textContent ?? ''
const buttons = () => [...host.querySelectorAll('button')].map(b => b.textContent?.trim() ?? '')

describe('🔴 请求必须带上市场 (串市场比报错难查得多)', () => {
  it('港股: api.rpsRotation 收到 market=HK 且 level=undefined', async () => {
    await mount('HK')
    expect(apiMock.rpsRotation).toHaveBeenCalledWith(12, 'industry', undefined, 'HK')
  })

  it('A 股行业: 默认 2 级, market=CN', async () => {
    await mount('CN')
    expect(apiMock.rpsRotation).toHaveBeenCalledWith(12, 'industry', 2, 'CN')
  })

  it('A 股概念: level 恒为 undefined (概念没有层级概念)', async () => {
    await mount('CN', 'concept', payload('概念'))
    expect(apiMock.rpsRotation).toHaveBeenCalledWith(12, 'concept', undefined, 'CN')
  })

  it('🔴 两个市场的 query key 不撞车 (缓存串市场会渲染出上一个市场的矩阵)', async () => {
    await mount('CN')
    const cnKey = client.getQueryCache().getAll()[0].queryKey
    await act(async () => root.unmount())
    host.remove()
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    await mount('HK')
    const hkKey = client.getQueryCache().getAll()[0].queryKey
    expect(JSON.stringify(cnKey)).not.toBe(JSON.stringify(hkKey))
    expect(cnKey).toContain('CN')
    expect(hkKey).toContain('HK')
  })
})

describe('行业层级选择器: 只在「行业名分层」的市场渲染', () => {
  it('A 股行业: 渲染 1/2/3 级', async () => {
    await mount('CN')
    for (const lv of ['1级', '2级', '3级']) expect(buttons()).toContain(lv)
  })

  it('港股行业: 不渲染 (恒生行业单层, 后端 _rotation_hk 忽略 level)', async () => {
    await mount('HK')
    for (const lv of ['1级', '2级', '3级']) expect(buttons()).not.toContain(lv)
  })

  it('A 股概念: 不渲染 (kind 不是 industry)', async () => {
    await mount('CN', 'concept', payload('概念'))
    for (const lv of ['1级', '2级', '3级']) expect(buttons()).not.toContain(lv)
  })

  it('渲染与否与 ROTATION_SUPPORT.industryLevels 一致 (表是唯一事实源)', async () => {
    for (const m of ['CN', 'HK'] as Market[]) {
      await mount(m)
      const shown = buttons().includes('2级')
      expect(shown, `${m}`).toBe(ROTATION_SUPPORT[m].industryLevels)
      await act(async () => root.unmount())
      host.remove()
      host = document.createElement('div')
      document.body.appendChild(host)
      root = createRoot(host)
      client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    }
  })
})

describe('AI 轮动分析区块: 无大盘背景源的市场只渲染说明', () => {
  it('A 股: 渲染「生成分析」按钮', async () => {
    await mount('CN')
    expect(buttons()).toContain('生成分析')
    expect(host.querySelector('input[type="text"]')).not.toBeNull()
  })

  it('🔴 港股: 不渲染「生成分析」, 改为渲染替代说明', async () => {
    await mount('HK')
    expect(buttons()).not.toContain('生成分析')
    expect(text()).toContain(ROTATION_SUPPORT.HK.aiAnalysisHint!)
  })

  it('🔴 港股: 不渲染「关注点」输入框 (没有按钮, 留着只是个填了没用的框)', async () => {
    await mount('HK')
    expect(host.querySelector('input[type="text"]')).toBeNull()
    // 工具栏的天数滑块与市场无关, 两个市场都要在 (顺带证明上面那条不是「整块没渲染」)
    expect(host.querySelector('input[type="range"]')).not.toBeNull()
  })

  it('港股: 说明里点明缺的是大盘背景, 且不会拿 A 股背景冒充', async () => {
    await mount('HK')
    expect(text()).toContain('大盘背景')
    expect(text()).toContain('冒充')
  })
})

describe('空态文案: 指向该市场真实存在的入口', () => {
  it('🔴 有数据时矩阵真的渲染出来 (否则下面几条空态断言全是空转)', async () => {
    await mount('HK')
    expect(text()).toContain('行业A')
    expect(text()).toContain('行业B')
    expect(text()).not.toContain(ROTATION_SUPPORT.HK.emptyHint('行业'))
  })

  it('A 股: 让用户去板块页配置并获取数据源 (扩展数据通道)', async () => {
    await mount('CN', 'industry', EMPTY)
    expect(text()).toContain('配置并获取行业数据源')
  })

  it('🔴 港股: 不再叫用户去「配置并获取数据源」(港股行业随快照下发, 没有扩展数据表可配)', async () => {
    await mount('HK', 'industry', EMPTY)
    expect(text()).not.toContain('配置并获取')
    expect(text()).toContain('港股')
    expect(text()).toContain('数据 → 港股数据')
  })

  it('港股: 文案里的维度名跟着 kind 走 (概念/行业共用一个函数)', async () => {
    await mount('HK', 'industry', EMPTY)
    expect(text()).toContain('港股行业轮动数据')
  })
})

describe('标题与维度文案', () => {
  it('行业维度标题是「行业涨幅轮动」', async () => {
    await mount('HK')
    expect(host.querySelector('#rps-rotation-title')?.textContent).toBe('行业涨幅轮动')
  })

  it('概念维度标题是「概念涨幅轮动」', async () => {
    await mount('CN', 'concept', payload('概念'))
    expect(host.querySelector('#rps-rotation-title')?.textContent).toBe('概念涨幅轮动')
  })

  it('🔴 A 股 AI 区块的引导语用维度名, 不写死「概念」(行业态曾经文案串味)', async () => {
    await mount('CN', 'industry')
    expect(text()).toContain('天的行业轮动')
    expect(text()).not.toContain('天的概念轮动')
  })
})
