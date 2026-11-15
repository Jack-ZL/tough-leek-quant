// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { DEFAULT_MARKET, MARKETS, isMarket, normalizeMarket } from './market'
import { MARKET_CONTENT, coreIndexes, indexDisplayName, marketContent } from './marketContent'

describe('normalizeMarket: 展示层兜底 CN, 容忍大小写与空白', () => {
  it('已登记市场原样通过', () => {
    expect(normalizeMarket('CN')).toBe('CN')
    expect(normalizeMarket('HK')).toBe('HK')
  })

  it('大小写与首尾空白被归一', () => {
    expect(normalizeMarket('hk')).toBe('HK')
    expect(normalizeMarket('  Hk ')).toBe('HK')
  })

  it.each([['US'], [''], ['CN '], [null], [undefined], [42], [{}], [['HK']]])(
    '未知/非字符串 %s → CN(不白屏)',
    (input) => {
      expect(normalizeMarket(input)).toBe(DEFAULT_MARKET)
    },
  )

  it('isMarket 大小写敏感(先 toUpperCase 再判, 与后端 symbol 归一同理)', () => {
    expect(isMarket('HK')).toBe(true)
    expect(isMarket('hk')).toBe(false)
    expect(isMarket('US')).toBe(false)
    expect(isMarket(null)).toBe(false)
  })
})

describe('MARKET_CONTENT: 每个市场都有内容, 且与后端 index_const.py 对齐', () => {
  it('MARKETS 里每个市场都在表里(Record 类型的运行时兜底)', () => {
    for (const m of MARKETS) {
      expect(MARKET_CONTENT[m]?.market).toBe(m)
    }
    expect(Object.keys(MARKET_CONTENT).sort()).toEqual([...MARKETS].sort())
  })

  it('CN 核心四只 = 上证/深成/创业板/科创综指(顺序即展示顺序)', () => {
    expect(coreIndexes('CN').map(i => i.symbol)).toEqual([
      '000001.SH', '399001.SZ', '399006.SZ', '000680.SH',
    ])
    expect(coreIndexes('CN').map(i => i.name)).toEqual([
      '上证指数', '深证成指', '创业板指', '科创综指',
    ])
  })

  it('HK 核心四只 = HSI/HSTECH/HSCEI/HSCCI, 代码为源生形不带 .HK', () => {
    expect(coreIndexes('HK').map(i => i.symbol)).toEqual(['HSI', 'HSTECH', 'HSCEI', 'HSCCI'])
    expect(coreIndexes('HK').map(i => i.name)).toEqual([
      '恒生指数', '恒生科技指数', '恒生中国企业指数', '恒生香港中资企业指数',
    ])
    // 🔴 港股指数没有交易所后缀这一说, 加 .HK 两个源都查不到
    for (const i of coreIndexes('HK')) expect(i.symbol).not.toContain('.')
  })

  it('缺省 market = CN', () => {
    expect(coreIndexes()).toEqual(coreIndexes('CN'))
    expect(marketContent().label).toBe('A股')
  })

  it('两个市场的核心指数集合不重叠(串市场会立刻被这条拦下)', () => {
    const cn = new Set(coreIndexes('CN').map(i => i.symbol))
    for (const i of coreIndexes('HK')) expect(cn.has(i.symbol)).toBe(false)
  })
})

describe('短名: 唯一且不超侧栏 85px 预算', () => {
  // 侧栏指数条两列布局下单卡内容区仅 85px, 4 字中文名 + 涨跌幅已接近占满
  // (Layout.tsx 注释记录过「上证指/数」折行事故)。
  const MAX_DISPLAY_CHARS = 4

  it.each(MARKETS)('%s 的展示名互不相同', (m) => {
    const names = coreIndexes(m).map(indexDisplayName)
    expect(new Set(names).size).toBe(names.length)
  })

  it.each(MARKETS)('%s 的展示名不超过 %d 字', (m) => {
    for (const i of coreIndexes(m)) {
      expect(indexDisplayName(i).length).toBeLessThanOrEqual(MAX_DISPLAY_CHARS)
    }
  })

  it('indexDisplayName 无短名时回退权威全名', () => {
    expect(indexDisplayName({ symbol: '000001.SH', name: '上证指数' })).toBe('上证指数')
    expect(indexDisplayName({ symbol: 'HSCEI', name: '恒生中国企业指数', shortName: '国企指数' }))
      .toBe('国企指数')
  })

  it('🔴 回归: 恒生科技与国企指数不得缩写成同一个词(review/Page.tsx 曾双双压成「恒生」)', () => {
    const bySymbol = Object.fromEntries(coreIndexes('HK').map(i => [i.symbol, indexDisplayName(i)]))
    expect(bySymbol.HSTECH).not.toBe(bySymbol.HSCEI)
    expect(bySymbol.HSTECH).toBe('恒生科技')
    expect(bySymbol.HSCEI).toBe('国企指数')
  })
})
