// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ klineDaily: vi.fn(), hkDaily: vi.fn() }))

vi.mock('@/lib/api', () => ({ api: mocks }))
vi.mock('./StockPanel', () => ({
  StockPanel: ({ symbol }: { symbol: string }) => <span>A股主体 {symbol}</span>,
}))
vi.mock('./HkStockDailyPanel', () => ({
  HkStockDailyPanel: ({ symbol, source }: { symbol: string; source: { market: string } }) =>
    <span>港股主体 {symbol} {source.market}</span>,
}))

import { klineSource } from '@/lib/kline-market-adapter'
import { symbolMarket } from '@/lib/marketContent'
import { StockDetailBody } from './StockDetailBody'

let host: HTMLDivElement, root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); host.remove() })

const render = async (symbol: string, source = klineSource(symbolMarket(symbol))) =>
  act(async () => { root.render(<StockDetailBody symbol={symbol} source={source} />) })

it('按市场查表选主体, 调用方不写 market 分支(方案 §8 规则 2)', async () => {
  await render('600519.SH')
  expect(host.textContent).toBe('A股主体 600519.SH')
  await render('00700.HK')
  expect(host.textContent).toBe('港股主体 00700.HK HK')
})

it('🔴 认不出的代码不渲染主体, 也**不兜底 A 股**', async () => {
  // 兜底等于拿港股/美股代码去打 A 股端点, 然后显示一张空图 —— 比如实报错难发现得多。
  await render('AAPL')
  expect(host.textContent).toContain('无法识别的证券代码：AAPL')
  expect(host.textContent).not.toContain('A股主体')
  expect(host.querySelector('.text-danger')).not.toBeNull()
})

it('source 为 null 时不去查 BODIES(避免 undefined 组件崩溃)', async () => {
  await render('00700.HK', null)
  expect(host.textContent).toContain('无法识别的证券代码：00700.HK')
  expect(host.textContent).not.toContain('港股主体')
})
