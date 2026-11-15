import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import {
  closeStockPreview, openStockPreview,
} from './stockPreview'
import { getStockPreviewRequest, navigateStockPreview, stockPreviewUi, subscribeStockPreview } from '@/lib/stockPreviewStore'

beforeEach(() => { vi.stubGlobal('window', { __TSP_EXT__: { ui: stockPreviewUi } }) })
afterEach(() => { stockPreviewUi.closeStockPreview(); vi.unstubAllGlobals() })

it('returns false on an older host and remains safe before browser bootstrap', () => {
  vi.stubGlobal('window', { __TSP_EXT__: { api: {} } })
  expect(openStockPreview({ symbol: '159558.SZ' })).toBe(false)
  expect(() => closeStockPreview()).not.toThrow()
  vi.stubGlobal('window', undefined)
  expect(openStockPreview({ symbol: '159558.SZ' })).toBe(false)
})

it.each(['605365.SH', '159558.SZ', '000001.SH', '920071.BJ', '00700.HK', '00001.HK'])(
  'accepts the existing full symbol contract: %s', symbol => {
  expect(openStockPreview({ symbol })).toBe(true)
  expect(getStockPreviewRequest()?.symbol).toBe(symbol)
})

// 🔴 回归: 详情弹窗是 K线/详情的**唯一入口**, 而 `stockPreviewStore.normalizeItem`
// 曾写死 A 股正则 `/^\d{6}\.(SH|SZ|BJ)$/` —— 港股代码在那道墙前被静默丢弃,
// 表现为「点港股没反应」, 没有任何报错。判定改为遍历 marketContent 的表。
it('港股代码能打开详情(旧实现写死 A 股正则, 会静默返回 false)', () => {
  expect(openStockPreview({ symbol: ' 00700.hk ', name: '腾讯控股' })).toBe(true)
  expect(getStockPreviewRequest()).toMatchObject({ symbol: '00700.HK', name: '腾讯控股' })
  // 同一份 navList 里混两个市场也照单全收: 归一化按 symbol 逐个判, 不按列表首个
  expect(openStockPreview({
    symbol: '00700.HK', navList: [{ symbol: '00700.HK' }, { symbol: '600519.SH' }, { symbol: 'AAPL.US' }],
  })).toBe(true)
  expect(getStockPreviewRequest()?.navList?.map(i => i.symbol)).toEqual(['00700.HK', '600519.SH'])
})

it('normalizes and copies inputs without guessing an exchange or retaining caller-owned objects', () => {
  const navList = [
    { symbol: ' 159558.sz ', name: ' ETF ' },
    { symbol: '159558.SZ', name: '重复' },
    { symbol: '605365.SH', name: null },
    { symbol: '605365' },
  ]
  expect(openStockPreview({ symbol: ' 159558.sz ', navList })).toBe(true)
  expect(getStockPreviewRequest()).toMatchObject({
    symbol: '159558.SZ', name: 'ETF', navList: [
      { symbol: '159558.SZ', name: 'ETF' }, { symbol: '605365.SH' },
    ],
  })
  navList[0].symbol = '600519.SH'
  expect(getStockPreviewRequest()?.navList?.[0].symbol).toBe('159558.SZ')
})

it.each([null, undefined, {}, { symbol: 123 }, { symbol: '600519' }, { symbol: '../settings' },
  { symbol: '600519.US' }, { symbol: '600519.SH', navList: {} },
  // 🔴 位数不对的港股代码: 6 位数字 + .HK 既不合 A 股(后缀白名单不含 HK)也不合
  // 港股(要求 5 位)。后端 `market._SYMBOL_PATTERN_BODY` 同一条注释警告过: 用宽松的
  // `[A-Z]{2}` 会让 `007000.HK` 被判成合法 A 股并流进 A 股链路。
  { symbol: '007000.HK' }, { symbol: '700.HK' }, { symbol: '00700.HKX' }, { symbol: '00700' },
])('rejects malformed runtime inputs without replacing an open preview: %j', input => {
  openStockPreview({ symbol: '159558.SZ' })
  const previous = getStockPreviewRequest()
  const changed = vi.fn()
  const unsubscribe = subscribeStockPreview(changed)
  try {
    expect(openStockPreview(input as Parameters<typeof openStockPreview>[0])).toBe(false)
    expect(getStockPreviewRequest()).toBe(previous)
    expect(changed).not.toHaveBeenCalled()
  } finally { unsubscribe() }
})

it('replaces the current request, navigates with the same list and closes idempotently', () => {
  openStockPreview({ symbol: '159558.SZ', navList: [{ symbol: '159558.SZ' }, { symbol: '605365.SH', name: '立达信' }] })
  const list = getStockPreviewRequest()!.navList
  navigateStockPreview('605365.SH', '立达信')
  expect(getStockPreviewRequest()).toMatchObject({ symbol: '605365.SH', name: '立达信', navList: list })
  openStockPreview({ symbol: '600519.SH' })
  expect(getStockPreviewRequest()?.navList).toBeUndefined()
  const changed = vi.fn()
  const unsubscribe = subscribeStockPreview(changed)
  closeStockPreview()
  closeStockPreview()
  navigateStockPreview('159558.SZ')
  expect(getStockPreviewRequest()).toBeNull()
  expect(changed).toHaveBeenCalledOnce()
  unsubscribe()
})
