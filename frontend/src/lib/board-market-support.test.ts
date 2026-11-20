import { describe, expect, it } from 'vitest'
import { MARKETS, type Market } from './market'
import {
  BOARD_DIMENSION_SOURCE,
  BOARD_NO_SOURCE_COPY,
  ROTATION_SUPPORT,
  boardDimensionSource,
  boardNoSourceCopy,
  rotationSupport,
  type BoardKind,
} from './board-market-support'

const KINDS: BoardKind[] = ['concept', 'industry']

describe('🔴 完整性: 每个市场都必须显式声明 (fail-closed)', () => {
  // Record<Market, …> 已经让「漏一个市场」变成编译错误; 这里再钉住运行时形状,
  // 防止有人用 `as` 或索引签名绕过类型检查。
  it.each(KINDS)('%s: 维度源表声明的市场集合恰好等于 MARKETS', (kind) => {
    expect(Object.keys(BOARD_DIMENSION_SOURCE[kind]).sort()).toEqual([...MARKETS].sort())
  })

  it.each(KINDS)('%s: 文案表声明的市场集合恰好等于 MARKETS', (kind) => {
    expect(Object.keys(BOARD_NO_SOURCE_COPY[kind]).sort()).toEqual([...MARKETS].sort())
  })

  it.each(KINDS)('%s: 维度源要么是 null, 要么是带 kind 的完整对象', (kind) => {
    for (const m of MARKETS) {
      const src = BOARD_DIMENSION_SOURCE[kind][m]
      if (src === null) continue
      expect(['ext-data', 'snapshot-field']).toContain(src.kind)
      if (src.kind === 'snapshot-field') {
        // 少了 field 名, groupsFromSnapshotField 会静默拿到 undefined → 整页空组;
        // 少了 label, 页面副标题只能退回泛称「行业」, 用户看不出这是恒生行业口径。
        expect(typeof src.field).toBe('string')
        expect(src.field.length).toBeGreaterThan(0)
        expect(typeof src.label).toBe('string')
        expect(src.label.length).toBeGreaterThan(0)
      }
    }
  })

  it.each(KINDS)('%s: 文案要么是 null, 要么是完整文案对象', (kind) => {
    for (const m of MARKETS) {
      const copy = BOARD_NO_SOURCE_COPY[kind][m]
      if (copy === null) continue
      expect(typeof copy.title).toBe('string')
      expect(typeof copy.hint).toBe('string')
      expect(copy.title.length).toBeGreaterThan(0)
      expect(copy.hint.length).toBeGreaterThan(0)
    }
  })
})

describe('🔴 两张表必须一致: 有源就不许有空态文案, 无源就必须有', () => {
  // 这是本模块唯一不能靠人记的约束。两种失配都是真 bug:
  //   有源 + 有文案 → 页面明明拿得到数据却整页空态 (港股行业曾经就是这样);
  //   无源 + 无文案 → 页面静默渲染成一片空白, 用户以为「暂无数据」反复点刷新。
  it.each(KINDS)('%s: 维度源为 null ⟺ 文案非 null', (kind) => {
    for (const m of MARKETS) {
      const noSource = BOARD_DIMENSION_SOURCE[kind][m] === null
      expect(noSource, `${kind}/${m}`).toBe(BOARD_NO_SOURCE_COPY[kind][m] !== null)
    }
  })

  it('boardNoSourceCopy 以维度源表为准 (文案表忘了同步也不会误判成无源)', () => {
    for (const kind of KINDS) {
      for (const m of MARKETS) {
        const expected = BOARD_DIMENSION_SOURCE[kind][m] === null
          ? BOARD_NO_SOURCE_COPY[kind][m]
          : null
        expect(boardNoSourceCopy(kind, m), `${kind}/${m}`).toBe(expected)
      }
    }
  })

  it('boardDimensionSource 与表本体一致 (helper 不做额外兜底)', () => {
    for (const kind of KINDS) {
      for (const m of MARKETS) {
        expect(boardDimensionSource(kind, m)).toBe(BOARD_DIMENSION_SOURCE[kind][m])
      }
    }
  })
})

describe('A 股: 两个板块页都走扩展数据通道', () => {
  it.each(KINDS)('%s: CN 有源且无空态文案', (kind) => {
    expect(boardDimensionSource(kind, 'CN')).toEqual({ kind: 'ext-data' })
    expect(boardNoSourceCopy(kind, 'CN')).toBeNull()
  })
})

describe('港股行业: 有源 (恒生行业随行情快照下发)', () => {
  it('维度源指向快照的 industry 列, 并自带分类人类名', () => {
    // 🔴 港股没有同花顺行业表, 分类只能随 market-snapshot 下发; 数据本来就在
    // instruments_hk 里 (东财 f100), 且被 _HK_PRESERVE_FIELDS 保护不被上游抖动掏空。
    // label 放在表里而不是页面写死「恒生行业」, 守住「市场差异是数据不是控制流」。
    expect(boardDimensionSource('industry', 'HK')).toEqual({
      kind: 'snapshot-field', field: 'industry', label: '恒生行业',
    })
  })

  it('不渲染空态文案 (早先这里一律判「无源」是错的)', () => {
    expect(boardNoSourceCopy('industry', 'HK')).toBeNull()
  })

  it('🔴 与概念板块结论不同不是笔误, 而是两个市场事实', () => {
    // 行业有本地真数据; 概念确实无源 (东财板块中心全是 A 股, akshare 港股只有人气榜)。
    expect(boardDimensionSource('industry', 'HK')).not.toBeNull()
    expect(boardDimensionSource('concept', 'HK')).toBeNull()
  })
})

describe('港股概念: 无源 → 明示 + 不用 A 股数据冒充 (方案 §3.4 / §11)', () => {
  const hkConcept = boardNoSourceCopy('concept', 'HK')!

  it('标题就是方案要求的那句话', () => {
    // docs/hk-market-plan.md:587「前端隐藏 + 明示『港股无概念板块数据源』」
    expect(hkConcept.title).toBe('港股无概念板块数据源')
  })

  it('文案说明是「无数据源」而不是「暂无数据」', () => {
    // 「暂无数据」会被读成「同步一下就有」, 用户会反复点刷新; 必须说清是**没有源**。
    expect(hkConcept.title).toContain('无')
    expect(hkConcept.title).toContain('数据源')
    expect(hkConcept.hint).toContain('不渲染')
    expect(hkConcept.hint).not.toContain('暂无数据')
    expect(hkConcept.hint).not.toContain('稍后')
  })

  it('文案带上实测依据, 而不是空口断言', () => {
    expect(hkConcept.hint).toContain('东财板块中心')
    expect(hkConcept.hint).toContain('akshare')
    expect(hkConcept.hint).toContain('人气榜')
  })

  it('文案给出扩展数据通道这条后路 (方案: 零代码改动即可接入)', () => {
    expect(hkConcept.hint).toContain('扩展数据')
    expect(hkConcept.hint).toContain('无需改代码')
  })

  it('文案告诉用户怎么回到 A 股 (侧栏切换器是全局市场状态的唯一入口)', () => {
    expect(hkConcept.hint).toContain('市场切换器')
  })

  it('🔴 文案不复用行业板块的措辞 (防止将来只改一处)', () => {
    // 行业页已有源, 这里钉的是「概念文案里的板块名必须是概念」,
    // 避免 HK_NO_SOURCE_HINT 被写成硬编码的「行业」而两个页面共用。
    expect(hkConcept.hint).toContain('概念')
    expect(hkConcept.hint).not.toContain('行业板块')
  })
})

describe('类型契约: 新增市场必须显式回答', () => {
  // 这条不是运行时断言, 而是把 Record<Market, …> 的编译期约束写成可读的说明:
  // 若有人往 lib/market.ts 的 MARKETS 加了 'US' 而没补这两张表, `npx tsc -b` 会失败。
  it('MARKETS 至少含 CN 与 HK (否则上面的完整性断言会空转)', () => {
    expect(MARKETS).toContain('CN')
    expect(MARKETS).toContain('HK')
  })

  it('两张表的键都与 Market 类型一一对应', () => {
    for (const kind of KINDS) {
      const declared = Object.keys(BOARD_DIMENSION_SOURCE[kind]) as Market[]
      expect(declared.every(m => MARKETS.includes(m))).toBe(true)
      const copies = Object.keys(BOARD_NO_SOURCE_COPY[kind]) as Market[]
      expect(copies.every(m => MARKETS.includes(m))).toBe(true)
    }
  })
})

// ================================================================
// 轮动对话框能力表 (ROTATION_SUPPORT)
// ================================================================

describe('🔴 ROTATION_SUPPORT: 每个市场都必须显式声明', () => {
  it('声明的市场集合恰好等于 MARKETS', () => {
    expect(Object.keys(ROTATION_SUPPORT).sort()).toEqual([...MARKETS].sort())
  })

  it('每个市场的形状完整 (布尔位 + 文案函数)', () => {
    for (const m of MARKETS) {
      const s = ROTATION_SUPPORT[m]
      expect(typeof s.industryLevels, `${m}.industryLevels`).toBe('boolean')
      expect(typeof s.aiAnalysis, `${m}.aiAnalysis`).toBe('boolean')
      expect(typeof s.emptyHint, `${m}.emptyHint`).toBe('function')
    }
  })

  it('aiAnalysis 为 false ⟺ 必须给出替代说明 (不许留空白区块)', () => {
    // 两种失配都是真 bug:
    //   aiAnalysis=false 且 hint=null → 对话框上半区整块消失, 用户以为功能被删了;
    //   aiAnalysis=true  且 hint≠null → 说明文案永远渲染不出来, 是死代码。
    for (const m of MARKETS) {
      const s = ROTATION_SUPPORT[m]
      expect(s.aiAnalysisHint !== null, `${m}.aiAnalysisHint`).toBe(!s.aiAnalysis)
      if (s.aiAnalysisHint !== null) expect(s.aiAnalysisHint.length).toBeGreaterThan(0)
    }
  })

  it('emptyHint 把维度名嵌进文案 (两个维度共用一个函数, 不许写死)', () => {
    for (const m of MARKETS) {
      for (const dimLabel of ['概念', '行业']) {
        expect(ROTATION_SUPPORT[m].emptyHint(dimLabel), `${m}/${dimLabel}`).toContain(dimLabel)
      }
    }
  })

  it('rotationSupport() 返回表里的同一份对象', () => {
    for (const m of MARKETS) expect(rotationSupport(m)).toBe(ROTATION_SUPPORT[m])
  })
})

describe('A 股: 层级选择器 + AI 分析都在, 空态文案逐字不变', () => {
  const cn = ROTATION_SUPPORT.CN

  it('行业名是 "一级-二级-三级" 路径 → 渲染 1/2/3 级选择器', () => {
    expect(cn.industryLevels).toBe(true)
  })

  it('有大盘背景源 (CN_RULES.has_market_overview=true) → 渲染 AI 分析', () => {
    expect(cn.aiAnalysis).toBe(true)
    expect(cn.aiAnalysisHint).toBeNull()
  })

  it('🔴 空态文案与改造前对话框里内联的那句逐字一致', () => {
    // 这条钉住「把内联文案搬进表」没有顺手改掉 A 股用户看到的字。
    expect(cn.emptyHint('行业')).toBe('暂无行业数据,请先在「行业板块」页配置并获取行业数据源')
    expect(cn.emptyHint('概念')).toBe('暂无概念数据,请先在「概念板块」页配置并获取概念数据源')
  })
})

describe('港股: 无层级、无 AI 分析, 空态指向港股同步而不是扩展数据配置', () => {
  const hk = ROTATION_SUPPORT.HK

  it('恒生行业是单层分类 → 不渲染 1/2/3 级选择器', () => {
    // 后端 _rotation_hk 忽略 level 并把它钉成 None; 前端若还摆着选择器,
    // 用户点了三级却看不到任何变化, 会以为数据坏了。
    expect(hk.industryLevels).toBe(false)
  })

  it('无大盘背景源 (HK_RULES.has_market_overview=false) → 不渲染 AI 分析', () => {
    expect(hk.aiAnalysis).toBe(false)
  })

  it('AI 说明讲清「缺什么」而不是「暂不支持」了事', () => {
    expect(hk.aiAnalysisHint).toContain('大盘背景')
    expect(hk.aiAnalysisHint).toContain('A 股')
  })

  it('🔴 AI 说明明说不会拿 A 股背景冒充港股 (fail closed)', () => {
    expect(hk.aiAnalysisHint).toContain('冒充')
  })

  it('🔴 空态文案不再叫港股用户去「配置并获取数据源」', () => {
    // 这是本次要修的原话: 港股行业分类随行情快照下发, **没有扩展数据表可配**,
    // 旧文案会把用户引到一个在港股态根本不渲染的「配置数据源」按钮上。
    const hint = hk.emptyHint('行业')
    expect(hint).not.toContain('配置并获取')
    expect(hint).toContain('港股')
    expect(hint).toContain('同步')
  })

  it('空态文案给出真实入口 (数据 → 港股数据)', () => {
    expect(hk.emptyHint('行业')).toContain('数据 → 港股数据')
  })
})
