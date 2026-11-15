// @vitest-environment node
// 复盘页指数紧凑名: 权威在 lib/marketContent.ts 的 CoreIndex.compactName, 本页只做派生。
// 🔴 回归背景: 本页曾自带第三份硬编码缩写表, 回退算法是「去掉 指数/成指/A股/综指/50
// 再截前两字」, 于是「恒生科技指数」和「恒生中国企业指数」**双双压成「恒生」** ——
// 摘要条上两条并列显示同一个词, 用户无法区分。
import { describe, expect, it } from 'vitest'
import { indexShort, shortenIndexNames } from './Page'

describe('indexShort: 走 marketContent 权威表, 不再自己猜', () => {
  it('A股四大指数仍是单字(存量摘要条布局不变)', () => {
    expect(indexShort('上证指数', '000001.SH')).toBe('上')
    expect(indexShort('深证成指', '399001.SZ')).toBe('深')
    expect(indexShort('创业板指', '399006.SZ')).toBe('创')
    expect(indexShort('科创综指', '000680.SH')).toBe('科')
  })

  it('🔴 回归: 恒生科技指数与恒生中国企业指数不得同词', () => {
    const tech = indexShort('恒生科技指数', 'HSTECH')
    const cei = indexShort('恒生中国企业指数', 'HSCEI')
    expect(tech).toBe('恒科')
    expect(cei).toBe('国企')
    expect(tech).not.toBe(cei)
  })

  it('港股四条核心指数紧凑名两两不同', () => {
    const names = [
      indexShort('恒生指数', 'HSI'),
      indexShort('恒生科技指数', 'HSTECH'),
      indexShort('恒生中国企业指数', 'HSCEI'),
      indexShort('恒生香港中资企业指数', 'HSCCI'),
    ]
    expect(new Set(names).size).toBe(4)
  })

  it('只有代码没有名字时按代码查表', () => {
    expect(indexShort(null, 'HSTECH')).toBe('恒科')
    expect(indexShort(undefined, '000001.SH')).toBe('上')
  })

  it('名字与代码都查不到 → 回退去后缀截两字(存量报告里的旧名)', () => {
    expect(indexShort('中证白酒指数', '399997.SZ')).toBe('中证')
  })

  it('名字与代码都缺 → 占位符, 不抛', () => {
    expect(indexShort(null, undefined)).toBe('—')
    expect(indexShort(undefined, undefined)).toBe('—')
  })

  it('存量别名「科创50」仍映射到「科」(不在核心指数表里)', () => {
    expect(indexShort('科创50', '000688.SH')).toBe('科')
  })
})

describe('shortenIndexNames: 批量重写存量 summary 文本', () => {
  it('A股全名被替换成单字', () => {
    expect(shortenIndexNames('上证指数+0.52%|深证成指-0.31%'))
      .toBe('上+0.52%|深-0.31%')
  })

  it('🔴 长名优先: 恒生科技指数不得被「恒生指数」先命中成「恒指科技指数」', () => {
    expect(shortenIndexNames('恒生科技指数+1.20%')).toBe('恒科+1.20%')
    expect(shortenIndexNames('恒生指数+0.80%')).toBe('恒指+0.80%')
  })

  it('港股全名同样被替换(存量文本可能含港股段)', () => {
    expect(shortenIndexNames('恒生中国企业指数-2.00%'))
      .toBe('国企-2.00%')
  })

  it('不含指数名的文本原样返回', () => {
    expect(shortenIndexNames('情绪 62|涨停 45 家')).toBe('情绪 62|涨停 45 家')
  })

  it('🔴 全局正则可重复调用(lastIndex 不被上一次调用污染)', () => {
    const text = '上证指数+0.52%|深证成指-0.31%'
    expect(shortenIndexNames(text)).toBe(shortenIndexNames(text))
  })
})
