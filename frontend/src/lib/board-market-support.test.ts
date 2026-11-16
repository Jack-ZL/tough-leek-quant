import { describe, expect, it } from 'vitest'
import { MARKETS, type Market } from './market'
import { BOARD_NO_SOURCE_COPY, boardNoSourceCopy, type BoardKind } from './board-market-support'

const KINDS: BoardKind[] = ['concept', 'industry']

describe('🔴 完整性: 每个市场都必须显式声明 (fail-closed)', () => {
  // Record<Market, …> 已经让「漏一个市场」变成编译错误; 这里再钉住运行时形状,
  // 防止有人用 `as` 或索引签名绕过类型检查。
  it.each(KINDS)('%s: 声明的市场集合恰好等于 MARKETS', (kind) => {
    expect(Object.keys(BOARD_NO_SOURCE_COPY[kind]).sort()).toEqual([...MARKETS].sort())
  })

  it.each(KINDS)('%s: 每个市场的值要么是 null, 要么是完整文案对象', (kind) => {
    for (const m of MARKETS) {
      const copy = BOARD_NO_SOURCE_COPY[kind][m]
      if (copy === null) continue
      expect(typeof copy.title).toBe('string')
      expect(typeof copy.hint).toBe('string')
      expect(copy.title.length).toBeGreaterThan(0)
      expect(copy.hint.length).toBeGreaterThan(0)
    }
  })

  it('boardNoSourceCopy 与表本体一致 (helper 不做额外兜底)', () => {
    for (const kind of KINDS) {
      for (const m of MARKETS) {
        expect(boardNoSourceCopy(kind, m)).toBe(BOARD_NO_SOURCE_COPY[kind][m])
      }
    }
  })
})

describe('A 股有源, 正常渲染', () => {
  it.each(KINDS)('%s: CN 返回 null', (kind) => {
    expect(boardNoSourceCopy(kind, 'CN')).toBeNull()
  })
})

describe('港股无源: 明示 + 不用 A 股数据冒充 (方案 §3.4 / §11)', () => {
  const hkConcept = boardNoSourceCopy('concept', 'HK')!
  const hkIndustry = boardNoSourceCopy('industry', 'HK')!

  it('概念板块标题就是方案要求的那句话', () => {
    // docs/hk-market-plan.md:587「前端隐藏 + 明示『港股无概念板块数据源』」
    expect(hkConcept.title).toBe('港股无概念板块数据源')
  })

  it('行业板块同口径 (东财 t:2 的 496 个行业同样全是 A 股)', () => {
    expect(hkIndustry.title).toBe('港股无行业板块数据源')
  })

  it.each([['概念', hkConcept], ['行业', hkIndustry]] as [string, { title: string; hint: string }][])(
    '%s: 文案说明是「无数据源」而不是「暂无数据」',
    (_label, copy) => {
      // 「暂无数据」会被读成「同步一下就有」, 用户会反复点刷新; 必须说清是**没有源**。
      expect(copy.title).toContain('无')
      expect(copy.title).toContain('数据源')
      expect(copy.hint).toContain('不渲染')
      expect(copy.hint).not.toContain('暂无数据')
      expect(copy.hint).not.toContain('稍后')
    },
  )

  it.each([['概念', hkConcept], ['行业', hkIndustry]] as [string, { title: string; hint: string }][])(
    '%s: 文案带上实测依据, 而不是空口断言',
    (_label, copy) => {
      expect(copy.hint).toContain('东财板块中心')
      expect(copy.hint).toContain('akshare')
      expect(copy.hint).toContain('人气榜')
    },
  )

  it.each([['概念', hkConcept], ['行业', hkIndustry]] as [string, { title: string; hint: string }][])(
    '%s: 文案给出扩展数据通道这条后路 (方案: 零代码改动即可接入)',
    (_label, copy) => {
      expect(copy.hint).toContain('扩展数据')
      expect(copy.hint).toContain('无需改代码')
    },
  )

  it('文案告诉用户怎么回到 A 股 (侧栏切换器是全局市场状态的唯一入口)', () => {
    expect(hkConcept.hint).toContain('市场切换器')
    expect(hkIndustry.hint).toContain('市场切换器')
  })

  it('🔴 两个板块页的文案不共用同一份标题 (防止只改一处)', () => {
    expect(hkConcept.title).not.toBe(hkIndustry.title)
    expect(hkConcept.hint).not.toBe(hkIndustry.hint)
  })
})

describe('类型契约: 新增市场必须显式回答', () => {
  // 这条不是运行时断言, 而是把 Record<Market, …> 的编译期约束写成可读的说明:
  // 若有人往 lib/market.ts 的 MARKETS 加了 'US' 而没补这里, `npx tsc -b` 会失败。
  it('MARKETS 至少含 CN 与 HK (否则上面的完整性断言会空转)', () => {
    expect(MARKETS).toContain('CN')
    expect(MARKETS).toContain('HK')
  })

  it('表键与 Market 类型一一对应', () => {
    const declared = Object.keys(BOARD_NO_SOURCE_COPY.concept) as Market[]
    expect(declared.every(m => MARKETS.includes(m))).toBe(true)
  })
})
