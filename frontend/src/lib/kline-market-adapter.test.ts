// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  klineDaily: vi.fn(),
  hkDaily: vi.fn(),
}))

vi.mock('@/lib/api', () => ({ api: mocks }))

import { KLINE_SOURCES, klineSource } from './kline-market-adapter'
import { QK } from './queryKeys'
import { MARKETS } from './market'
import { marketContent, marketToday } from './marketContent'

const cn = KLINE_SOURCES.CN
const hk = KLINE_SOURCES.HK
const RANGE = { start: '2025-01-01', end: '2025-06-30' }

beforeEach(() => {
  vi.clearAllMocks()
  mocks.klineDaily.mockResolvedValue({ symbol: '600519.SH', rows: [] })
  mocks.hkDaily.mockResolvedValue({
    symbol: '00700.HK', name: '腾讯控股', market: 'HK', volume_unit: 'shares',
    currency: 'HKD', quote_delay_minutes: 15, empty_reason: null, rows: [],
  })
})

describe('klineSource: 市场 → 数据源查表, 无控制流分支', () => {
  it('MARKETS 里每个市场都在表里(Record 类型的运行时兜底)', () => {
    expect(Object.keys(KLINE_SOURCES).sort()).toEqual([...MARKETS].sort())
    for (const m of MARKETS) expect(KLINE_SOURCES[m].market).toBe(m)
  })

  it.each(MARKETS)('%s 的 content 与 marketContent 同源', (m) => {
    expect(KLINE_SOURCES[m].content).toBe(marketContent(m))
  })

  it('🔴 认不出的市场返回 null, **不兜底 CN**(与 indexSource 刻意相反)', () => {
    // 本函数决定打哪个端点, 属请求层: 兜底等于拿港股代码去查 A 股, 静默返回空。
    expect(klineSource(null)).toBeNull()
    expect(klineSource('CN')).toBe(cn)
    expect(klineSource('HK')).toBe(hk)
  })
})

describe('query key: 两个市场前缀互斥, 跨市场不得沿用缓存', () => {
  it('日K key 前缀不同', () => {
    expect(cn.dailyQueryOptions('X', RANGE).queryKey[0]).not.toBe(hk.dailyQueryOptions('X', RANGE).queryKey[0])
    expect(hk.dailyQueryOptions('X', RANGE).queryKey[0]).toBe('hk-daily')
  })

  it('港股 key 走 QK.hkDaily(带 limit), 与刷新按钮失效的 key 同前缀', () => {
    const key = hk.dailyQueryOptions('00700.HK', RANGE).queryKey
    expect(key[0]).toBe(QK.hkDaily('00700.HK', RANGE.start, RANGE.end, 500)[0])
    expect(key).toEqual(QK.hkDaily('00700.HK', RANGE.start, RANGE.end, 500))
    expect(hk.refreshKeys('00700.HK', 'daily')[0][0]).toBe(key[0])
  })

  it('港股 placeholderData 拒绝 A 股 prev key(只比 symbol 会串市场)', () => {
    const { placeholderData } = hk.dailyQueryOptions('00700.HK', RANGE)
    const prev = { rows: [{ date: '2025-01-01' }] }
    const qk = (queryKey: readonly unknown[]) => ({ queryKey })
    // 同市场同 symbol → 沿用(改日期范围时不闪)
    expect(placeholderData!(prev, qk(QK.hkDaily('00700.HK', '2024-01-01', '2024-06-30', 500)))).toBe(prev)
    // 🔴 A 股前缀 + 同一个 symbol 字符串 → 必须丢弃
    expect(placeholderData!(prev, qk(['kline', '00700.HK', 'a', 'b']))).toBeUndefined()
    // 同市场不同 symbol → 丢弃
    expect(placeholderData!(prev, qk(QK.hkDaily('00001.HK', RANGE.start, RANGE.end, 500)))).toBeUndefined()
    expect(placeholderData!(prev, qk(undefined as unknown as readonly unknown[]))).toBeUndefined()
    expect(placeholderData!(prev, undefined)).toBeUndefined()
  })

  it('A 股 placeholderData 保持原语义: 同 symbol 跨日期范围可暂显', () => {
    const { placeholderData } = cn.dailyQueryOptions('600519.SH', RANGE)
    const prev = { rows: [] }
    expect(placeholderData!(prev, { queryKey: QK.kline('600519.SH', '2024-01-01', '2024-06-30') })).toBe(prev)
    expect(placeholderData!(prev, { queryKey: QK.kline('000001.SZ', '2024-01-01', '2024-06-30') })).toBeUndefined()
  })
})

describe('queryFn: 各打各的端点, 参数如实透传', () => {
  it('A 股 → api.klineDaily', async () => {
    await cn.dailyQueryOptions('600519.SH', RANGE).queryFn()
    expect(mocks.klineDaily).toHaveBeenCalledWith('600519.SH', undefined, RANGE, undefined)
    expect(mocks.hkDaily).not.toHaveBeenCalled()
  })

  it('港股 → api.hkDaily(带 start/end/limit)', async () => {
    await hk.dailyQueryOptions('00700.HK', RANGE).queryFn()
    expect(mocks.hkDaily).toHaveBeenCalledWith('00700.HK', {
      start: RANGE.start, end: RANGE.end, limit: 500,
    })
    expect(mocks.klineDaily).not.toHaveBeenCalled()
  })
})

describe('capabilities: 未就绪的能力直接隐藏入口(方案 §8 规则 4)', () => {
  it('A 股四项全开', () => {
    expect(cn.capabilities).toEqual({ minute: true, watchlist: true, monitor: true, abnormal: true })
  })

  it('港股: 无实时行情链路 ⇒ 分时/监控/异动关; 自选已接通 ⇒ 开', () => {
    expect(hk.capabilities).toEqual({ minute: false, watchlist: true, monitor: false, abnormal: false })
  })

  it('能力表字段齐全(新增能力时两个市场都必须表态, 否则 TS 报缺 key)', () => {
    for (const m of MARKETS) {
      expect(Object.keys(KLINE_SOURCES[m].capabilities).sort())
        .toEqual(['abnormal', 'minute', 'monitor', 'watchlist'])
    }
  })
})

describe('boardTag: 板块小标是市场内容, 不是通用工具', () => {
  it.each([
    ['300750.SZ', '创'], ['301236.SZ', '创'], ['688981.SH', '科'], ['430047.BJ', '北'], ['830799.BJ', '北'],
  ])('A 股 %s → %s', (symbol, label) => {
    expect(cn.boardTag(symbol)?.label).toBe(label)
  })

  it('A 股主板无小标', () => {
    expect(cn.boardTag('600519.SH')).toBeNull()
    expect(cn.boardTag('000001.SZ')).toBeNull()
  })

  // 钉住**搬迁前就有**的行为: 北交所新号段 920xxx 不匹配 /^[48]/, 因此没有小标。
  // 这是原弹窗 boardTag 的既有缺口, 本次只是原样搬进适配器(不顺手改无关行为);
  // 真要补号段应当连同 Screener 列表那份副本一起改, 另开一次改动。
  it('A 股 920 号段北交所目前无小标(既有行为, 原样搬迁)', () => {
    expect(cn.boardTag('920071.BJ')).toBeNull()
  })

  it('🔴 回归: 港股 8 开头是创业板, 沿用 A 股 /^[48]/ 会误标成「北」', () => {
    // A 股规则下这两个代码都会被判成北交所
    expect(cn.boardTag('80001.HK')?.label).toBe('北')
    expect(cn.boardTag('40001.HK')?.label).toBe('北')
    // 港股一律无板块小标(5 位代码前缀不承载板块语义)
    expect(hk.boardTag('80001.HK')).toBeNull()
    expect(hk.boardTag('40001.HK')).toBeNull()
    expect(hk.boardTag('00700.HK')).toBeNull()
  })
})

describe('refreshKeys: 刷新按钮失效的 key 必须与本市场端点对应', () => {
  it('A 股按视图分别失效日K / 分钟K', () => {
    expect(cn.refreshKeys('600519.SH', 'daily')).toEqual([['kline', '600519.SH']])
    expect(cn.refreshKeys('600519.SH', 'intraday')).toEqual([
      ['kline-minute-range', '600519.SH'], ['kline-minute', '600519.SH'],
    ])
  })

  it('🔴 港股只失效 hk-daily: 写死 A 股 key 会让刷新按钮点了没反应', () => {
    for (const view of ['daily', 'intraday'] as const) {
      const keys = hk.refreshKeys('00700.HK', view)
      expect(keys).toEqual([['hk-daily', '00700.HK']])
      for (const key of keys) expect(key[0]).not.toBe('kline')
    }
  })
})

describe('dailyEmptyHint: 空态即契约(方案 §8 规则 3), 必须解释为什么 + 给出出路', () => {
  it('港股按 empty_reason 分两种文案, 且都指路到「数据」页', () => {
    const noDaily = hk.dailyEmptyHint('no_daily')
    const noEnriched = hk.dailyEmptyHint('no_enriched')
    expect(noDaily).not.toBe(noEnriched)
    expect(noDaily).toContain('尚未同步')
    // enriched 缺失是被**刻意**不回退原始价的结果, 文案必须说清并提到完整性凭据
    expect(noEnriched).toContain('enriched')
    expect(noEnriched).toContain('coverage.json')
    for (const hint of [noDaily, noEnriched]) expect(hint).toContain('数据')
  })

  it('港股 empty_reason 为 null/未知值时回退到「尚未同步」而不是空字符串', () => {
    expect(hk.dailyEmptyHint(null)).toBe(hk.dailyEmptyHint('no_daily'))
    expect(hk.dailyEmptyHint(undefined)).toBe(hk.dailyEmptyHint('no_daily'))
    expect(hk.dailyEmptyHint('whatever').length).toBeGreaterThan(0)
  })

  it.each(MARKETS)('%s 的空态文案非空', (m) => {
    expect(KLINE_SOURCES[m].dailyEmptyHint(null).length).toBeGreaterThan(0)
  })
})

describe('today: 按市场时区取「今天」', () => {
  it.each(MARKETS)('%s 与 marketToday 同口径', (m) => {
    const instant = new Date('2025-01-01T17:00:00Z')
    expect(KLINE_SOURCES[m].today(instant)).toBe(marketToday(m, instant))
  })

  it('缺省 now = 当前时刻(不传参也要能算)', () => {
    for (const m of MARKETS) expect(KLINE_SOURCES[m].today()).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })
})
