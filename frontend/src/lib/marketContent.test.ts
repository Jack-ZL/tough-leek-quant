// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { DEFAULT_MARKET, MARKETS, isMarket, normalizeMarket } from './market'
import { MARKET_CONTENT, coreIndexFullNames, coreIndexes, indexCompactName, indexDisplayName, marketContent, marketToday, symbolMarket } from './marketContent'

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

describe('紧凑名 compactName: 极窄处(1~2 字位)展示名', () => {
  // 消费方: custom/review/Page.tsx 的市场摘要条(一行塞四大指数 + 情绪分 + 涨跌结构)。
  const MAX_COMPACT_CHARS = 2

  it.each(MARKETS)('%s 的紧凑名互不相同', (m) => {
    const names = coreIndexes(m).map(i => i.compactName).filter(Boolean)
    expect(new Set(names).size).toBe(names.length)
  })

  it.each(MARKETS)('%s 每条核心指数都登记了紧凑名(否则复盘页会退回「去后缀截两字」的撞名算法)', (m) => {
    for (const i of coreIndexes(m)) {
      expect(i.compactName, `${i.name} 缺 compactName`).toBeTruthy()
      expect(i.compactName!.length).toBeLessThanOrEqual(MAX_COMPACT_CHARS)
    }
  })

  it('🔴 回归: 恒生科技指数与恒生中国企业指数的紧凑名不得相同', () => {
    expect(indexCompactName({ name: '恒生科技指数' })).toBe('恒科')
    expect(indexCompactName({ name: '恒生中国企业指数' })).toBe('国企')
    expect(indexCompactName({ name: '恒生科技指数' }))
      .not.toBe(indexCompactName({ name: '恒生中国企业指数' }))
  })

  it('A股紧凑名沿用单字(存量摘要条布局不变)', () => {
    expect(indexCompactName({ name: '上证指数' })).toBe('上')
    expect(indexCompactName({ symbol: '399006.SZ' })).toBe('创')
  })

  it('查不到返回 null, 不猜、不截字', () => {
    expect(indexCompactName({ name: '中证白酒指数', symbol: '399997.SZ' })).toBeNull()
    expect(indexCompactName({})).toBeNull()
    expect(indexCompactName({ name: null, symbol: null })).toBeNull()
  })
})

describe('coreIndexFullNames: 供消费方拼批量替换正则', () => {
  it('🔴 按长度降序 —— 否则「恒生指数」会先于「恒生科技指数」命中', () => {
    const names = coreIndexFullNames()
    for (let i = 1; i < names.length; i++) {
      expect(names[i - 1].length).toBeGreaterThanOrEqual(names[i].length)
    }
    expect(names.indexOf('恒生科技指数')).toBeLessThan(names.indexOf('恒生指数'))
  })

  it('跨市场去重且覆盖两个市场的核心指数', () => {
    const names = coreIndexFullNames()
    expect(new Set(names).size).toBe(names.length)
    expect(names).toContain('上证指数')
    expect(names).toContain('恒生中国企业指数')
  })
})

// ===== symbolPattern / timeZone: 后端权威的只读镜像(见文件头 🟡 例外段) =====

describe('symbolPattern: 与后端 market._SYMBOL_PATTERN_BODY 逐字对齐', () => {
  // 后端 `_SYMBOL_PATTERN_BODY` 是**不带锚点**的片段, 前端负责加 ^…$。
  // 这两条字符串是契约: 改一边不改另一边 = 港股代码流进 A 股链路(或反之),
  // 而且**静默**——后端只会返回空结果, 前端只会显示一张空图。
  const BACKEND_BODY: Record<string, string> = {
    CN: '[0-9]{6}\\.(?:BJ|SH|SZ)',
    HK: '[0-9]{5}\\.HK',
  }

  it.each(MARKETS)('%s 的正则 = 后端片段加锚点', (m) => {
    expect(MARKET_CONTENT[m].symbolPattern.source).toBe(`^${BACKEND_BODY[m]}$`)
  })

  it('🔴 不得带 g 标志: RegExp.test 带 g 会推进 lastIndex, 第二次调用就漏判', () => {
    for (const m of MARKETS) {
      const re = MARKET_CONTENT[m].symbolPattern
      expect(re.global).toBe(false)
      expect(re.flags).not.toContain('g')
      // 同一个正则对象连判三次同一个合法代码, 结果必须稳定
      const sample = m === 'HK' ? '00700.HK' : '600519.SH'
      expect([re.test(sample), re.test(sample), re.test(sample)]).toEqual([true, true, true])
      expect(re.lastIndex).toBe(0)
    }
  })

  it('🔴 用交易所后缀白名单而不是宽松的 [A-Z]{2}: 007000.HK 不得被判成合法 A 股', () => {
    // 后端同一条注释点名的事故: 宽松后缀会让 6 位数字 + .HK 通过 A 股校验
    expect(MARKET_CONTENT.CN.symbolPattern.test('007000.HK')).toBe(false)
    expect(symbolMarket('007000.HK')).toBeNull()
  })
})

describe('symbolMarket: 过滤/请求层专用, 认不出返回 null 且绝不兜底 CN', () => {
  it.each([
    ['600519.SH', 'CN'], ['000001.SZ', 'CN'], ['920071.BJ', 'CN'], ['159558.SZ', 'CN'],
    ['00700.HK', 'HK'], ['00001.HK', 'HK'], ['99888.HK', 'HK'],
  ] as const)('%s → %s', (symbol, expected) => {
    expect(symbolMarket(symbol)).toBe(expected)
  })

  it('大小写与首尾空白被归一(与后端 symbol 归一同理)', () => {
    expect(symbolMarket(' 00700.hk ')).toBe('HK')
    expect(symbolMarket('600519.sh')).toBe('CN')
  })

  it.each([
    ['007000.HK'], // 6 位 + .HK: 两个市场都不合
    ['700.HK'],    // 位数不足
    ['00700'],     // 缺交易所后缀 —— 不按位数猜市场
    ['600519'],    // 同上(A 股)
    ['AAPL.US'],   // 未登记市场
    ['600519.US'],
    ['../settings'],
    [''],
  ])('认不出 → null(%s), 不兜底 CN', (symbol) => {
    expect(symbolMarket(symbol)).toBeNull()
  })

  it.each([[null], [undefined], [42], [{}], [['00700.HK']]])('非字符串 %s → null 而不抛', (input) => {
    expect(symbolMarket(input)).toBeNull()
  })

  it('🔴 与 normalizeMarket 的兜底行为相反(展示层兜底 / 过滤层不兜底)', () => {
    expect(normalizeMarket('US')).toBe(DEFAULT_MARKET)
    expect(symbolMarket('600519.US')).toBeNull()
  })

  it('两个市场的合法代码集合不重叠(串市场会立刻被这条拦下)', () => {
    for (const m of MARKETS) {
      const other = MARKETS.filter(x => x !== m)
      const sample = m === 'HK' ? '00700.HK' : '600519.SH'
      for (const o of other) {
        expect(MARKET_CONTENT[o].symbolPattern.test(sample)).toBe(false)
      }
    }
  })
})

describe('timeZone / marketToday: 按市场时区取「今天」, 不是 UTC 也不是共用常量', () => {
  it.each(MARKETS)('%s 的 IANA 时区与后端 MarketTradingRules.iana_timezone 一致', (m) => {
    const expected: Record<string, string> = { CN: 'Asia/Shanghai', HK: 'Asia/Hong_Kong' }
    expect(MARKET_CONTENT[m].timeZone).toBe(expected[m])
    // Intl 认得这个时区名(拼错会静默回退成 UTC, 表现为日期差一天)
    expect(() => new Intl.DateTimeFormat('en-CA', { timeZone: MARKET_CONTENT[m].timeZone })).not.toThrow()
  })

  it('🔴 两个市场各存一份时区, 不共用常量: 「都是 UTC+8」是巧合不是契约', () => {
    expect(MARKET_CONTENT.CN.timeZone).not.toBe(MARKET_CONTENT.HK.timeZone)
  })

  it('🔴 不用 toISOString().slice(0,10): 那是 UTC 日期, 东八区傍晚会错一天', () => {
    // 该 UTC 时刻在东八区已经是次日
    const instant = new Date('2025-01-01T17:00:00Z')
    expect(instant.toISOString().slice(0, 10)).toBe('2025-01-01')
    for (const m of MARKETS) expect(marketToday(m, instant)).toBe('2025-01-02')
  })

  it('marketToday 输出 YYYY-MM-DD(en-CA 格式), 且缺省 now = 当前时刻', () => {
    const iso = /^\d{4}-\d{2}-\d{2}$/
    for (const m of MARKETS) {
      expect(marketToday(m, new Date('2025-06-30T04:00:00Z'))).toMatch(iso)
      expect(marketToday(m)).toMatch(iso)
    }
  })
})
