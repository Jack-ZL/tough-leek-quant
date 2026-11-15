// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  indexQuotes: vi.fn(),
  indexDaily: vi.fn(),
  syncIndexDaily: vi.fn(),
  hkIndices: vi.fn(),
  hkIndexDaily: vi.fn(),
  hkSyncIndexDaily: vi.fn(),
}))

vi.mock('@/lib/api', () => ({ api: mocks }))

import { INDEX_SOURCES, indexPageParams, indexSource } from './index-market-adapter'
import { MARKETS } from './market'
import { coreIndexes } from './marketContent'

const cn = INDEX_SOURCES.CN
const hk = INDEX_SOURCES.HK

beforeEach(() => {
  vi.clearAllMocks()
  mocks.indexQuotes.mockResolvedValue({ rows: [], count: 0 })
  mocks.indexDaily.mockResolvedValue({ symbol: '000001.SH', rows: [] })
  mocks.hkIndices.mockResolvedValue({
    rows: [], count: 0, source: 'realtime', market: 'HK',
    quote_delay_minutes: 15, currency: 'HKD',
  })
  mocks.hkIndexDaily.mockResolvedValue({ symbol: 'HSI', name: '恒生指数', volume_unit: null, rows: [] })
})

describe('indexSource: 市场 → 数据源查表, 无控制流分支', () => {
  it.each(MARKETS)('%s 取到对应实现', (m) => {
    expect(indexSource(m).market).toBe(m)
  })

  it.each([[null], [undefined], ['US'], ['hk']])('未知/小写 %s 走 normalizeMarket', (input) => {
    expect(indexSource(input)).toBe(input === 'hk' ? hk : cn)
  })

  it('每个市场都有内容且 pinned 与 marketContent 同源', () => {
    for (const m of MARKETS) {
      const s = INDEX_SOURCES[m]
      expect(s.content.market).toBe(m)
      expect(s.pinned).toEqual(coreIndexes(m))
      expect(s.pinned.length).toBeGreaterThan(0)
    }
  })

  it('两个市场的 query key 前缀互不相同(切市场时据此丢弃 placeholderData)', () => {
    expect(cn.quotesKey[0]).not.toBe(hk.quotesKey[0])
    expect(cn.dailyKey('X', 'a', 'b')[0]).not.toBe(hk.dailyKey('X', 'a', 'b')[0])
  })

  it('同步后只失效本市场的缓存, 不跨市场串味', () => {
    const cnKeys = cn.invalidateKeys.map(k => k[0])
    const hkKeys = hk.invalidateKeys.map(k => k[0])
    for (const k of hkKeys) expect(cnKeys).not.toContain(k)
    expect(hkKeys).toContain('hk-index-daily')
  })
})

describe('延时分钟数只能来自接口字段', () => {
  it('A 股实时行情 → null(徽章不渲染)', async () => {
    expect((await cn.fetchQuotes()).quoteDelayMinutes).toBeNull()
  })

  it.each([[15], [20], [0], [null]])(
    '港股原样透传 quote_delay_minutes=%s, 组件内无默认值',
    async (minutes) => {
      mocks.hkIndices.mockResolvedValue({
        rows: [], count: 4, source: 'realtime', quote_delay_minutes: minutes,
      })
      expect((await hk.fetchQuotes()).quoteDelayMinutes).toBe(minutes ?? null)
    },
  )

  it('港股只请求核心四只, 不是 27 只白名单全集', async () => {
    await hk.fetchQuotes()
    expect(mocks.hkIndices).toHaveBeenCalledWith(['HSI', 'HSTECH', 'HSCEI', 'HSCCI'])
  })
})

describe('港股日K必须排回升序(端点是 ORDER BY date DESC)', () => {
  it('DESC 输入 → 升序输出, 否则 K 线左右镜像', async () => {
    mocks.hkIndexDaily.mockResolvedValue({
      symbol: 'HSI', name: '恒生指数', volume_unit: null,
      rows: ['2026-11-13', '2026-11-12', '2026-11-11'].map(date => ({
        symbol: 'HSI', date, open: 1, high: 2, low: 0.5, close: 1.5, volume: 100,
      })),
    })
    const r = await hk.fetchDaily('HSI', { start: '2026-05-01', end: '2026-11-13' })
    expect(r.rows.map(x => x.date)).toEqual(['2026-11-11', '2026-11-12', '2026-11-13'])
    expect(r.name).toBe('恒生指数')
  })

  it('start/end/limit 透传给接口(参数名与 A 股不同: start/end 而非 start_date/end_date)', async () => {
    await hk.fetchDaily('HSTECH', { start: '2026-01-02', end: '2026-03-04' })
    expect(mocks.hkIndexDaily).toHaveBeenCalledWith('HSTECH', {
      start: '2026-01-02', end: '2026-03-04', limit: expect.any(Number),
    })
  })

  it('name 为 null 时不编造(非核心指数后端解析不出名字)', async () => {
    mocks.hkIndexDaily.mockResolvedValue({ symbol: 'VHSI', name: null, volume_unit: null, rows: [] })
    expect((await hk.fetchDaily('VHSI', { start: 'a', end: 'b' })).name).toBeNull()
  })
})

describe('数据源标签必须真实(方案 §8 规则 5)', () => {
  it('港股快照可用 → 标延时快照', () => {
    expect(hk.quoteSourceLabel({ rows: [], count: 4, source: 'realtime', quoteDelayMinutes: 15 }))
      .toContain('延时快照')
  })

  it('🔴 港股降级读本地日K → 必须如实说出来, 不能伪装成实时', () => {
    const label = hk.quoteSourceLabel({ rows: [], count: 4, source: 'index_daily', quoteDelayMinutes: 15 })
    expect(label).toContain('降级')
    expect(label).not.toContain('延时快照')
  })

  it('来源缺失 → 不猜', () => {
    expect(hk.quoteSourceLabel({ rows: [], count: 0, source: null, quoteDelayMinutes: null }))
      .toContain('来源未知')
    expect(hk.quoteSourceLabel(undefined)).toContain('0 只指数')
  })

  it('A 股沿用既有文案', () => {
    expect(cn.quoteSourceLabel({ rows: [], count: 4, source: null, quoteDelayMinutes: null }))
      .toBe('实时缓存 4 只指数')
  })
})

describe('分时能力与空态文案按市场分表', () => {
  it('港股无指数分时端点', () => {
    expect(hk.supportsMinute).toBe(false)
    expect(cn.supportsMinute).toBe(true)
    expect(hk.minuteUnavailableHint).toContain('港股')
  })

  it('港股日K空态解释了「为什么」并给出路(空态即契约)', () => {
    expect(hk.dailyEmptyHint).toContain('同步港股指数日K')
    expect(hk.dailyEmptyHint).toContain('14')
  })

  it('同步按钮文案区分市场', () => {
    expect(cn.syncLabel).toBe('同步指数日K')
    expect(hk.syncLabel).toBe('同步港股指数日K')
  })
})

describe('indexPageParams: 缺省市场不写进 URL', () => {
  it('A 股保持既有链接形态(侧栏指数条在用 /indices?symbol=…)', () => {
    expect(indexPageParams('CN', '000001.SH')).toEqual({ symbol: '000001.SH' })
    expect(indexPageParams('CN')).toEqual({})
  })

  it('港股带上 market, 刷新/分享不丢市场', () => {
    expect(indexPageParams('HK', 'HSI')).toEqual({ market: 'HK', symbol: 'HSI' })
    expect(indexPageParams('HK')).toEqual({ market: 'HK' })
  })
})

describe('同步动作按市场走各自端点', () => {
  it('A 股 → /api/index/sync_daily', async () => {
    await cn.syncDaily()
    expect(mocks.syncIndexDaily).toHaveBeenCalledTimes(1)
    expect(mocks.hkSyncIndexDaily).not.toHaveBeenCalled()
  })

  it('港股 → /api/hk/sync_index_daily', async () => {
    await hk.syncDaily()
    expect(mocks.hkSyncIndexDaily).toHaveBeenCalledTimes(1)
    expect(mocks.syncIndexDaily).not.toHaveBeenCalled()
  })
})
