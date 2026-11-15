// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  klineDaily: vi.fn(),
  hkDaily: vi.fn(),
  // useQuery 的返回值: 每个用例直接改这个对象, 模拟后端响应
  rq: { data: undefined as unknown },
}))

vi.mock('@/lib/api', () => ({ api: mocks }))
vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data: mocks.rq.data }) }))
vi.mock('@/components/QuoteDelayBadge', () => ({
  QuoteDelayBadge: ({ minutes }: { minutes?: number | null }) =>
    minutes ? <span>延时 {minutes} 分钟</span> : null,
}))
// 图表只透出本用例关心的注入点, 顺带把 getDefaultRange 钉成固定值
vi.mock('@/components/StockDailyKChart', () => ({
  getDefaultRange: () => ({ start: '2025-01-01', end: '2025-06-30' }),
  StockDailyKChart: (props: {
    symbol: string; dateRange?: unknown; queryOptions?: { queryKey: readonly unknown[] }
    emptyHint?: string; showLimitMarkers?: boolean
  }) => <output>{JSON.stringify({
    symbol: props.symbol,
    dateRange: props.dateRange,
    queryKey: props.queryOptions?.queryKey,
    emptyHint: props.emptyHint,
    showLimitMarkers: props.showLimitMarkers,
  })}</output>,
}))

import { KLINE_SOURCES } from '@/lib/kline-market-adapter'
import { HkStockDailyPanel } from './HkStockDailyPanel'

const hk = KLINE_SOURCES.HK
let host: HTMLDivElement, root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.rq.data = undefined
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); host.remove() })

const render = async (extra: Partial<Parameters<typeof HkStockDailyPanel>[0]> = {}) =>
  act(async () => { root.render(<HkStockDailyPanel symbol="00700.HK" source={hk} {...extra} />) })
const chart = () => JSON.parse(host.querySelector('output')!.textContent!)

it('把港股查询配置注入共用图表, 图表本身不认识市场(方案 §8 规则 2)', async () => {
  await render()
  expect(chart().queryKey[0]).toBe('hk-daily')
  expect(chart().queryKey).toContain('00700.HK')
  // 🔴 港股无涨跌停(后端 limit_* 恒 null): 显式关掉, 不依赖「反正算不出标记」
  expect(chart().showLimitMarkers).toBe(false)
  expect(chart().dateRange).toEqual({ start: '2025-01-01', end: '2025-06-30' })
  await render({ dateRange: { start: '2024-01-01', end: '2024-12-31' } })
  expect(chart().dateRange).toEqual({ start: '2024-01-01', end: '2024-12-31' })
})

it('空态即契约: 未同步与「enriched 未生成」给两套不同的处置指引', async () => {
  await render()
  expect(chart().emptyHint).toContain('港股日K尚未同步')
  mocks.rq.data = { empty_reason: 'no_enriched' }
  await render()
  // 这条必须说清「刻意不回退原始价」, 否则用户会以为是 bug 并要求回退
  expect(chart().emptyHint).toContain('刻意不回退原始价')
  expect(chart().emptyHint).toContain('coverage.json')
  mocks.rq.data = { empty_reason: null }
  await render()
  expect(chart().emptyHint).toContain('港股日K尚未同步')
})

it('🔴 延时分钟数只来自接口字段, 前端不写死 15', async () => {
  mocks.rq.data = { quote_delay_minutes: 15 }
  await render()
  expect(host.textContent).toContain('延时 15 分钟')
  mocks.rq.data = { quote_delay_minutes: null }
  await render()
  expect(host.textContent).not.toContain('延时')
})

it('🔴 量纲 shares→股: 标成「手」会让成交量被读成 100 倍', async () => {
  mocks.rq.data = { currency: 'HKD', volume_unit: 'shares' }
  await render()
  expect(host.textContent).toContain('HKD · 量纲 股')
  mocks.rq.data = { currency: 'HKD', volume_unit: 'lots' }
  await render()
  expect(host.textContent).toContain('量纲 手')
  // 未知值原样透出, 不猜
  mocks.rq.data = { currency: 'HKD', volume_unit: 'boards' }
  await render()
  expect(host.textContent).toContain('量纲 boards')
})

it('维表未同步时 name 为 null, 只显示代码不显示 undefined', async () => {
  mocks.rq.data = { name: null, currency: null, volume_unit: null }
  await render()
  expect(host.textContent).toContain('00700.HK')
  expect(host.textContent).not.toContain('undefined')
  expect(host.textContent).not.toContain('null')
  mocks.rq.data = { name: '腾讯控股', currency: 'HKD', volume_unit: 'shares' }
  await render()
  expect(host.textContent).toContain('腾讯控股')
})
