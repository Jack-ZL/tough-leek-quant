// `groupsFromSnapshotField` — 快照自带分类列 → DimensionGroup[]
//
// 这是港股行业板块的唯一维度来源 (A 股走 `resolveDimension` 的扩展数据通道),
// 所以它的丢弃规则必须单独钉住: 聚错组不会报错, 只会渲染出一个成分最多的假板块。
import { describe, expect, it } from 'vitest'
import { groupsFromSnapshotField } from './analysis-adapter'

const row = (symbol: string, industry: unknown, extra: Record<string, unknown> = {}) =>
  ({ symbol, industry, ...extra })

describe('聚组', () => {
  it('按分类列聚组, count 与 stocks 长度一致', () => {
    const groups = groupsFromSnapshotField([
      row('00700.HK', '资讯科技器材'),
      row('09988.HK', '资讯科技器材'),
      row('00005.HK', '银行'),
    ], 'industry')
    expect(groups.map(g => g.key)).toEqual(['资讯科技器材', '银行'])
    expect(groups.map(g => g.count)).toEqual([2, 1])
    expect(groups[0].stocks.map(s => s.symbol)).toEqual(['00700.HK', '09988.HK'])
  })

  it('按成分数降序 (热力图/龙头榜都依赖这个顺序)', () => {
    const groups = groupsFromSnapshotField([
      row('A', '小'), row('B', '大'), row('C', '大'), row('D', '大'),
    ], 'industry')
    expect(groups.map(g => g.key)).toEqual(['大', '小'])
  })

  it('分类名前后空白被 trim, 同一行业不因空格裂成两组', () => {
    const groups = groupsFromSnapshotField([
      row('A', '金融业'), row('B', '  金融业 '),
    ], 'industry')
    expect(groups).toHaveLength(1)
    expect(groups[0].key).toBe('金融业')
    expect(groups[0].count).toBe(2)
  })

  it('🔴 快照行本身就是行情, 原样带进 stocks (页面不再二次拼接 quoteMap)', () => {
    const groups = groupsFromSnapshotField([
      row('00700.HK', '资讯科技器材', { name: '腾讯控股', change_pct: 0.021, amount: 8e9 }),
    ], 'industry')
    expect(groups[0].stocks[0]).toMatchObject({
      symbol: '00700.HK', name: '腾讯控股', change_pct: 0.021, amount: 8e9,
    })
  })

  it('metrics 留空对象 (聚合指标由页面按市场口径自算, 适配层不猜)', () => {
    const groups = groupsFromSnapshotField([row('A', '银行')], 'industry')
    expect(groups[0].metrics).toEqual({})
  })
})

describe('🔴 丢弃规则: 宁可少几只标的, 不造假板块', () => {
  it.each([
    ['null', null],
    ['undefined (列缺失)', undefined],
    ['空串', ''],
    ['纯空白', '   '],
    ['数字', 123],
  ])('分类为 %s 的行被丢弃', (_label, industry) => {
    const groups = groupsFromSnapshotField([
      row('00700.HK', '资讯科技器材'), row('01234.HK', industry),
    ], 'industry')
    expect(groups).toHaveLength(1)
    expect(groups[0].key).toBe('资讯科技器材')
  })

  it.each(['nan', 'NaN', 'None', 'NULL', 'null'])('占位符 %s 被丢弃 (与扩展数据通道同口径)', (v) => {
    expect(groupsFromSnapshotField([row('A', v)], 'industry')).toEqual([])
  })

  it('🔴 绝不产出「未知」组 —— 它会因成分最多而永远霸占热度第一', () => {
    const groups = groupsFromSnapshotField([
      row('A', null), row('B', null), row('C', ''), row('D', 'nan'), row('E', '银行'),
    ], 'industry')
    expect(groups.map(g => g.key)).toEqual(['银行'])
    expect(groups.map(g => g.key).join()).not.toContain('未知')
  })

  it('没有 symbol 的行被丢弃 (拼不出行情, 渲染出来是空行)', () => {
    const groups = groupsFromSnapshotField([
      { industry: '银行' }, { symbol: '  ', industry: '银行' }, row('00005.HK', '银行'),
    ], 'industry')
    expect(groups[0].count).toBe(1)
    expect(groups[0].stocks[0].symbol).toBe('00005.HK')
  })

  it('整批都没有分类 → 返回空数组 (页面据此走「缺少行业分类」空态, 不是渲染 0 个板块)', () => {
    expect(groupsFromSnapshotField([row('A', null), row('B', undefined)], 'industry')).toEqual([])
  })
})

describe('边界', () => {
  it('空输入返回空数组', () => {
    expect(groupsFromSnapshotField([], 'industry')).toEqual([])
  })

  it('字段名不存在时返回空数组 (不抛异常, 也不误读别的列)', () => {
    const groups = groupsFromSnapshotField([{ symbol: 'A', sector: '银行' }], 'industry')
    expect(groups).toEqual([])
  })

  it('不修改入参 (页面把同一份 rows 同时喂给热力图和统计)', () => {
    const rows = [row('00700.HK', '资讯科技器材'), row('09988.HK', '资讯科技器材')]
    const snapshot = JSON.stringify(rows)
    groupsFromSnapshotField(rows, 'industry')
    expect(JSON.stringify(rows)).toBe(snapshot)
  })
})
