import { describe, it, expect } from 'vitest'
import { formatBeijingDateTime, formatMonthDay } from './MarketClock'

describe('formatMonthDay', () => {
  it('把 ISO 日期转为「M月D日」', () => {
    expect(formatMonthDay('2026-10-20')).toBe('10月20日')
    expect(formatMonthDay('2026-01-05')).toBe('1月5日')
  })

  it('非法输入原样返回', () => {
    expect(formatMonthDay('')).toBe('')
    expect(formatMonthDay('not-a-date')).toBe('not-a-date')
  })
})

describe('formatBeijingDateTime', () => {
  it('按北京时间 (UTC+8) 格式化 UTC 时刻', () => {
    // 2026-10-17T06:30:05Z = 北京 14:30:05
    expect(formatBeijingDateTime(new Date('2026-10-17T06:30:05Z'))).toBe('2026-10-17 14:30:05')
  })
})
