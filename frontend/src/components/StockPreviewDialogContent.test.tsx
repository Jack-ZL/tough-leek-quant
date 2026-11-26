// @vitest-environment jsdom
import { act, lazy } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import type { Preferences } from '@/lib/api'
import type { ColumnConfig } from '@/lib/stock-info-fields'
import type { FrontendSlotContextMap, FrontendSlotRegistration } from '@/extensions/types'
import type { Props } from './StockPreviewDialogContent'

vi.mock('./StockPanel', () => ({
  StockPanel: ({
    symbol, dateRange, infoBarOnly, customIndicators, visibleBars, onCustomizeInfoBar, showInfoBarCustomizer,
  }: {
    symbol: string
    dateRange: { start: string }
    infoBarOnly?: boolean
    customIndicators?: boolean
    visibleBars?: number | 'all'
    onCustomizeInfoBar?: () => void
    showInfoBarCustomizer?: boolean
  }) => (
    <>
      {infoBarOnly ? <span>行情</span> : (
        <span data-custom-indicators={!!customIndicators} data-visible-bars={visibleBars}>日线 {symbol} {dateRange.start}</span>
      )}
      {showInfoBarCustomizer !== false && onCustomizeInfoBar && (
        <button type="button" aria-label="自定义信息条" onClick={onCustomizeInfoBar} />
      )}
    </>
  ),
}))
vi.mock('./StockMultiDayIntradayChart', () => ({
  StockMultiDayIntradayChart: ({ symbol, days, customIndicators }: { symbol: string; days: number; customIndicators?: boolean }) =>
    <span data-custom-indicators={!!customIndicators}>分钟线 {symbol} {days}日</span>,
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
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
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
  const match = [...host.querySelectorAll('button')].find(el => el.textContent?.trim() === label || el.title === label)
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

it('个股详情的日 K 首屏保持 60 根，半年和一年只控制加载区间', async () => {
  const render = await setup()
  const initial = panel().textContent
  const visibleBars = () => panel().querySelector('[data-visible-bars]')?.getAttribute('data-visible-bars')
  expect(button('半年').getAttribute('aria-pressed')).toBe('true')
  expect(host.textContent).not.toContain('近60根')
  expect(visibleBars()).toBe('60')

  click('1年')
  const year = panel().textContent
  expect(year).not.toBe(initial)
  expect(visibleBars()).toBe('60')
  await render(second)
  expect(panel().textContent).toBe(year?.replace(first.symbol, second.symbol))
  expect(visibleBars()).toBe('60')

  click('半年')
  expect(panel().textContent).toBe(initial?.replace(first.symbol, second.symbol))
  expect(visibleBars()).toBe('60')
  click('1年')
  await render({ symbol: null })
  await render(first)
  expect(panel().textContent).toBe(initial)
  expect(button('半年').getAttribute('aria-pressed')).toBe('true')
  expect(visibleBars()).toBe('60')
})

it('在默认配置中保存首屏根数，失败保留草稿且加载区间不变', async () => {
  const { api } = await import('@/lib/api')
  vi.spyOn(api, 'preferences').mockImplementation(async () => client.getQueryData<Preferences>(QK.preferences)!)
  const save = vi.spyOn(api, 'updateStockPreviewDailyBars')
    .mockRejectedValueOnce(new Error('请重试'))
    .mockResolvedValue({ stock_preview_daily_bars: 85 })
  await setup()
  const display = () => panel().querySelector('[data-visible-bars]')?.getAttribute('data-visible-bars')
  const dialog = () => host.querySelector('[role="dialog"][aria-label="默认配置"]')
  const field = () => dialog()!.querySelector('input')!
  const initial = panel().textContent

  click('默认配置')
  expect(field().value).toBe('60')
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field(), '85')
    field().dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => button('保存').click())
  expect(save).toHaveBeenCalledWith(85, expect.anything())
  await vi.waitFor(async () => {
    await act(async () => {})
    expect(dialog()!.querySelector('[role="alert"]')?.textContent).toBe('请重试')
  })
  expect(field().value).toBe('85')
  expect(display()).toBe('60')

  await act(async () => button('保存').click())
  expect(save).toHaveBeenLastCalledWith(85, expect.anything())
  await vi.waitFor(async () => {
    await act(async () => {})
    expect(dialog()).toBeNull()
    expect(display()).toBe('85')
  })
  expect(panel().textContent).toBe(initial)
  click('默认配置')
  expect(field().value).toBe('85')
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(dialog()).toBeNull()
  expect(panel().textContent).toBe(initial)
})

it('日 K 和分时的信息条可以直接打开扩展字段编辑器', async () => {
  client.setQueryData(QK.extDataSchemaAll, { items: [] })
  await setup()
  const dialog = () => host.querySelector('[role="dialog"][aria-label="信息条指标"]')

  for (const view of ['日 K', '分时']) {
    click(view)
    const shortcut = panel().querySelector<HTMLButtonElement>('button[aria-label="自定义信息条"]')
    expect(shortcut).not.toBeNull()
    await act(async () => {
      shortcut!.click()
      await import('./ListColumnCustomizerInner')
    })
    expect(dialog()).not.toBeNull()

    await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })))
    expect(host.textContent).toContain(first.symbol)
    await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(dialog()).toBeNull()
    expect(button(view).getAttribute('aria-selected')).toBe('true')
  }
})

it('从齿轮添加和隐藏扩展字段，保存失败可重试并保留旧默认方案与日 K 首屏', async () => {
  const { api } = await import('@/lib/api')
  const old: ColumnConfig = {
    id: 'ext:sector:industry', label: '行业', visible: true,
    source: { type: 'ext', configId: 'sector', fieldName: 'industry' },
  }
  const layout = { fields: [old], baseline: [old] }
  client.setQueryData(QK.preferences, { stock_info_bar_layout: layout })
  vi.spyOn(api, 'preferences').mockImplementation(async () => client.getQueryData<Preferences>(QK.preferences)!)
  const schema = { items: [{
    id: 'sector', label: '行业与题材', mode: 'snapshot',
    columns: [
      { name: 'industry', label: '行业', type: 'TEXT' },
      { name: 'theme', label: '题材', type: 'TEXT' },
    ],
  }] }
  client.setQueryData(QK.extDataSchemaAll, schema)
  vi.spyOn(api, 'extDataSchemaAll').mockResolvedValue(schema)
  const save = vi.spyOn(api, 'updateStockInfoBarLayout')
    .mockRejectedValueOnce(new Error('保存失败，请重试'))
    .mockImplementation(async next => ({ stock_info_bar_layout: next }))

  await setup()
  const dialog = () => host.querySelector('[role="dialog"][aria-label="信息条指标"]')
  const row = (id: string) => dialog()?.querySelector<HTMLElement>('[data-col-row="' + id + '"]')
  const openCustomizer = async () => {
    click('默认配置')
    await act(async () => button('自定义信息条').click())
    await vi.waitFor(async () => {
      await act(async () => {})
      expect(dialog()).not.toBeNull()
    })
  }
  await openCustomizer()
  expect(row(old.id)).not.toBeNull()
  act(() => row(old.id)!.querySelector<HTMLButtonElement>('button[title="移出列表"]')!.click())
  expect(row(old.id)).toBeNull()
  act(() => [...dialog()!.querySelectorAll('button')].find(el => el.textContent?.includes('行业与题材'))!.click())
  click('题材TEXT')
  expect(row('ext:sector:theme')).not.toBeNull()
  expect(save).not.toHaveBeenCalled()

  await act(async () => button('保存').click())
  await vi.waitFor(async () => {
    await act(async () => {})
    expect(dialog()?.querySelector('[role="alert"]')?.textContent).toBe('保存失败，请重试')
  })
  expect(row('ext:sector:theme')).not.toBeNull()
  expect(client.getQueryData<Preferences>(QK.preferences)?.stock_info_bar_layout).toEqual(layout)

  await act(async () => button('保存').click())
  await vi.waitFor(async () => {
    await act(async () => {})
    expect(dialog()).toBeNull()
  })
  const saved = client.getQueryData<Preferences>(QK.preferences)?.stock_info_bar_layout
  expect(saved?.fields).toEqual(expect.arrayContaining([
    { ...old, visible: false }, expect.objectContaining({ id: 'ext:sector:theme', visible: true }),
  ]))
  expect(saved?.baseline).toEqual([old])
  expect(panel().querySelector('[data-visible-bars]')?.getAttribute('data-visible-bars')).toBe('60')
  await openCustomizer()
  expect(row(old.id)).toBeNull()
  expect(row('ext:sector:theme')).not.toBeNull()
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

it('日 K 和分时直接提供自定义指标，并可切换到扩展标签', async () => {
  const render = await setup([{ name: 'stock-preview.tabs', id: 'research', label: '研究', component: () => <span>扩展面板</span> }])
  const modeSwitch = () => [...host.querySelectorAll('button')].find(button => button.textContent?.trim() === '自定义指标')
  expect(modeSwitch()).toBeUndefined()
  expect(panel().querySelector('[data-custom-indicators="true"]')).not.toBeNull()
  click('研究')
  expect(panel().textContent).toBe('扩展面板')
  click('分时')
  expect(modeSwitch()).toBeUndefined()
  expect(panel().querySelector('[data-custom-indicators="true"]')).not.toBeNull()
  await render(second)
  expect(panel().querySelector('[data-custom-indicators="true"]')).not.toBeNull()
  await render({ symbol: null })
  await render(first)
  expect(button('日 K').getAttribute('aria-selected')).toBe('true')
  expect(panel().querySelector('[data-custom-indicators="true"]')).not.toBeNull()
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

/** 自选 / 加监控 / 异动条这三个入口是否渲染。
 *
 * 🔴 不再叫 `aShareOnlyAffordances`: 自选已接通港股(`capabilities.watchlist = true`),
 * 只有加监控与异动条仍是 A 股专属。 */
function marketAffordances() {
  return {
    watchlist: host.querySelector('[aria-label*="自选"]'),
    monitor: host.querySelector('[title="加监控"]'),
    abnormalBar: [...host.querySelectorAll('span')].find(el => el.textContent === '异动') ?? null,
  }
}

it('A 股详情保留自选与加监控入口(能力全开时的对照)', async () => {
  await setup()
  const affordances = marketAffordances()
  expect(affordances.watchlist).not.toBeNull()
  expect(affordances.monitor).not.toBeNull()
  expect(tabs()).toEqual(['日 K', '分时'])
})

it('港股详情: 分时/加监控入口隐藏, 自选入口保留, 主体走港股实现', async () => {
  const render = await setup()
  await render(hkFirst)
  // 分时 tab 被能力开关过滤掉(未就绪直接隐藏入口, 方案 §8 规则 4)
  expect(tabs()).toEqual(['日 K'])
  const affordances = marketAffordances()
  // 自选已接通(enriched 端点按代码分流到港股快照) ⇒ 入口必须出现;
  // 加监控 / 异动条依赖 A 股实时链路, 仍 fail-closed 隐藏。
  expect(affordances.watchlist).not.toBeNull()
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
