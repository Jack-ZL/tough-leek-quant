// @vitest-environment jsdom
import { act, lazy } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import type { FrontendSlotContextMap, FrontendSlotRegistration } from '@/extensions/types'
import type { Props } from './StockPreviewDialogContent'

vi.mock('./StockPanel', () => ({
  getDefaultRange: () => ({ start: '2025-01-01', end: '2025-06-30' }),
  StockPanel: ({ symbol, dateRange, infoBarOnly }: {
    symbol: string; dateRange: { start: string }; infoBarOnly?: boolean
  }) => infoBarOnly ? <span>行情</span> : <span>日线 {symbol} {dateRange.start}</span>,
}))
vi.mock('./StockMultiDayIntradayChart', () => ({
  StockMultiDayIntradayChart: ({ symbol, days }: { symbol: string; days: number }) =>
    <span>分钟线 {symbol} {days}日</span>,
}))
vi.mock('./monitor/RuleEditor', () => ({ RuleEditor: () => null }))
vi.mock('./HkStockDailyPanel', () => ({
  HkStockDailyPanel: ({ symbol, source }: {
    symbol: string; source: { content: { label: string } }
  }) => <span>港股日线 {symbol} {source.content.label}</span>,
}))

const first = { symbol: '600000.SH', name: '浦发银行' }
const second = { symbol: '000001.SZ' }
const hkFirst = { symbol: '00700.HK', name: '腾讯控股' }
let host: HTMLDivElement, root: Root, client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  localStorage.clear()
  client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  client.setQueryData(QK.watchlist, { symbols: [] })
  client.setQueryData(QK.monitorRules, { rules: [] })
  client.setQueryData(QK.customSignals, { signals: [] })
  client.setQueryData(QK.preferences, {})
})

afterEach(() => {
  act(() => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
  vi.resetModules()
})

async function setup(slots: FrontendSlotRegistration[] = []) {
  const registry = await import('@/extensions/registry')
  if (slots.length) {
    await registry.loadFrontendExtensions({
      example: async () => ({ default: { id: 'example', apiVersion: 1, slots } }),
    })
  }
  registry.finalizeFrontendExtensions(new Set())
  expect(registry.getFrontendExtensionLoadErrors()).toEqual([])
  const { StockPreviewDialogContent } = await import('./StockPreviewDialogContent')
  const render = (stock: Pick<Props, 'symbol' | 'name'>) => {
    root.render(<QueryClientProvider client={client}>
      <StockPreviewDialogContent {...stock} onClose={() => {}} navList={[first, second]}
        onNavigate={(symbol, name) => render({ symbol, name })} />
    </QueryClientProvider>)
  }
  await act(async () => render(first))
  return (stock: Pick<Props, 'symbol' | 'name'>) => act(async () => render(stock))
}

function button(label: string) {
  const match = [...host.querySelectorAll('button')].find(el => el.textContent === label || el.title === label)
  if (!match) throw new Error(`找不到按钮 ${label}`)
  return match
}

function click(label: string) {
  act(() => button(label).click())
}

function tabs() {
  return [...host.querySelectorAll('[role=tab]')].map(el => el.textContent)
}

function panel() {
  return host.querySelector<HTMLElement>('[role=tabpanel]')!
}

it('没有扩展时，日 K 和分时正常切换，保留各自区间并刷新对应行情', async () => {
  await setup()
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  expect(tabs()).toEqual(['日 K', '分时'])
  expect(button('日 K').getAttribute('aria-selected')).toBe('true')
  click('1年')
  const daily = panel().textContent
  expect(daily).toContain('日线 600000.SH')
  click('刷新')
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kline', first.symbol] })
  click('分时')
  click('5日')
  expect(panel().textContent).toContain('分钟线 600000.SH 5日')
  click('刷新')
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kline-minute-range', first.symbol] })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kline-minute', first.symbol] })
  click('日 K')
  expect(panel().textContent).toBe(daily)
  click('分时')
  expect(button('5日').getAttribute('aria-pressed')).toBe('true')
})

it('扩展按 order 和 id 追加，只有选中面板挂载，切股和重开遵循详情视图生命周期', async () => {
  const Research = vi.fn(({ symbol, name }: FrontendSlotContextMap['stock-preview.tabs']) =>
    <label>{symbol} {name ?? '无简称'}<input aria-label="草稿" /></label>)
  const Other = vi.fn(() => <div>其他面板</div>)
  const render = await setup([
    { name: 'stock-preview.tabs', id: 'daily', label: '研究', component: Research },
    { name: 'stock-preview.tabs', id: 'z', label: '附录', component: Other },
    { name: 'stock-preview.tabs', id: 'first', label: '概览', order: 1, component: Other },
    { name: 'stock-preview.footer', id: 'status', component: ({ view }) => <footer>{view}</footer> },
  ])
  expect(tabs()).toEqual(['日 K', '分时', '概览', '研究', '附录'])
  expect(Research).not.toHaveBeenCalled()
  expect(host.querySelector('footer')?.textContent).toBe('daily')
  click('研究')
  expect(panel().getAttribute('aria-labelledby')).toBe(button('研究').id)
  expect(panel().textContent).toBe('600000.SH 浦发银行')
  expect(host.querySelector('footer, [aria-label="分时周期"], [title="刷新"]')).toBeNull()
  expect(host.textContent).not.toContain('半年')
  panel().querySelector('input')!.value = '仅限第一只股票'
  await act(async () => {
    button('研究').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
  })
  expect(button('研究').getAttribute('aria-selected')).toBe('true')
  expect(panel().textContent).toBe('000001.SZ 无简称')
  expect(panel().querySelector('input')!.value).toBe('')
  expect(Other).not.toHaveBeenCalled()
  click('分时')
  expect(panel().textContent).toContain('分钟线 000001.SZ')
  expect(host.querySelector('footer')?.textContent).toBe('intraday')
  click('日 K')
  expect(panel().textContent).toContain('日线 000001.SZ')
  click('附录')
  expect(panel().textContent).toBe('其他面板')
  await render({ symbol: null })
  await render(first)
  expect(button('日 K').getAttribute('aria-selected')).toBe('true')
})

it('单个懒加载扩展的加载和渲染失败局限于面板，仍能切回内置图表', async () => {
  let fail!: (error: Error) => void
  const load = vi.fn(() => new Promise<{ default: () => null }>((_, reject) => { fail = reject }))
  await setup([{ name: 'stock-preview.tabs', id: 'broken', label: '研究', component: lazy(load) }])
  expect(load).not.toHaveBeenCalled()
  click('研究')
  expect(panel().querySelector('[role=status]')).not.toBeNull()
  const log = vi.spyOn(console, 'error').mockImplementation(() => {})
  await act(async () => fail(new Error('测试加载失败')))
  expect(panel().textContent).toContain('扩展 example 暂时不可用')
  expect(log).toHaveBeenCalled()
  click('分时')
  expect(panel().textContent).toContain('分钟线 600000.SH')
})

// ===== 港股详情: 能力开关驱动, 弹窗内没有一处 `market === 'HK'` =====

/** 自选 / 加监控 / 异动条这三个 A 股专属入口是否渲染。 */
function aShareOnlyAffordances() {
  return {
    watchlist: host.querySelector('[aria-label*="自选"]'),
    monitor: host.querySelector('[title="加监控"]'),
    abnormalBar: [...host.querySelectorAll('span')].find(el => el.textContent === '异动') ?? null,
  }
}

it('A 股详情保留自选与加监控入口(能力全开时的对照)', async () => {
  await setup()
  const affordances = aShareOnlyAffordances()
  expect(affordances.watchlist).not.toBeNull()
  expect(affordances.monitor).not.toBeNull()
  expect(tabs()).toEqual(['日 K', '分时'])
})

it('港股详情: 分时/自选/加监控入口全部隐藏, 主体走港股实现', async () => {
  const render = await setup()
  await render(hkFirst)
  // 分时 tab 被能力开关过滤掉(未就绪直接隐藏入口, 方案 §8 规则 4)
  expect(tabs()).toEqual(['日 K'])
  const affordances = aShareOnlyAffordances()
  expect(affordances.watchlist).toBeNull()
  expect(affordances.monitor).toBeNull()
  expect(affordances.abnormalBar).toBeNull()
  // 主体由 StockDetailBody 的注册表选中港股实现; A 股 StockPanel 的 mock 会把
  // getDefaultRange 的 start 渲染出来, 据此确认它没被挂载
  expect(panel().textContent).toContain('港股日线 00700.HK 港股')
  expect(panel().textContent).not.toContain('2025-01-01')
})

it('🔴 港股刷新失效 hk-daily 而不是 kline(写死 A 股 key 会点了没反应)', async () => {
  const render = await setup()
  await render(hkFirst)
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  click('刷新')
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['hk-daily', hkFirst.symbol] })
  for (const call of invalidate.mock.calls) {
    expect((call[0] as { queryKey: readonly unknown[] }).queryKey[0]).not.toBe('kline')
  }
})

it('在分时 tab 下切到港股会拉回日 K(该 tab 已被能力开关隐藏)', async () => {
  const render = await setup()
  click('分时')
  expect(panel().textContent).toContain('分钟线 600000.SH')
  await render(hkFirst)
  expect(tabs()).toEqual(['日 K'])
  expect(button('日 K').getAttribute('aria-selected')).toBe('true')
  expect(panel().textContent).toContain('港股日线 00700.HK')
  // 再切回 A 股, 分时 tab 与「保留当前视图」的规则都恢复
  await render(first)
  expect(tabs()).toEqual(['日 K', '分时'])
})

it('🔴 港股代码不挂 A 股板块小标(8 开头在港股是创业板, 不是北交所)', async () => {
  const render = await setup()
  await render({ symbol: '80001.HK' })
  expect(host.textContent).not.toContain('北')
  // 对照: 同样 4/8 开头的北交所代码在 A 股侧照旧挂「北」
  await render({ symbol: '430047.BJ' })
  expect(host.textContent).toContain('北')
})
