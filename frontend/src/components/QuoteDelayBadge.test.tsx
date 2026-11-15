// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { QuoteDelayBadge } from './QuoteDelayBadge'

let root: Root
let container: HTMLDivElement
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})
afterEach(() => { act(() => root.unmount()); container.remove() })

const render = (props: { minutes?: number | null; className?: string }) => {
  act(() => root.render(<QuoteDelayBadge {...props} />))
  return container.querySelector('span')
}

describe('QuoteDelayBadge: 分钟数只能来自接口字段', () => {
  it('有延时分钟数时渲染出该数字', () => {
    const el = render({ minutes: 15 })
    expect(el?.textContent).toBe('延时 15 分钟')
    expect(el?.getAttribute('aria-label')).toBe('延时 15 分钟')
  })

  it('分钟数随接口变化, 组件内无默认值', () => {
    expect(render({ minutes: 20 })?.textContent).toBe('延时 20 分钟')
    expect(render({ minutes: 1 })?.textContent).toBe('延时 1 分钟')
  })

  it.each([[null], [undefined], [0], [-1], [Number.NaN], [Number.POSITIVE_INFINITY]])(
    '空值/非正数/非有限 %s → 不渲染(A股无此字段, 不得凭空多出延时标注)',
    (minutes) => {
      expect(render({ minutes: minutes as number | null })).toBeNull()
    },
  )

  it('title 说清是授权延时而非故障', () => {
    expect(render({ minutes: 15 })?.getAttribute('title')).toContain('非数据源故障')
  })
})

describe('QuoteDelayBadge: 不得长得像告警', () => {
  // 🔴 回归护栏: QuoteFreshnessBadge 的 'delayed' 是**异常**态, 用 text-warning 琥珀色。
  // 港股延时是正常态, 若沿用告警配色, 用户会以为数据源坏了。
  it('不使用告警/危险配色', () => {
    const cls = render({ minutes: 15 })?.className ?? ''
    for (const banned of ['text-warning', 'text-danger', 'text-bear', 'amber', 'animate-pulse']) {
      expect(cls).not.toContain(banned)
    }
    expect(cls).toContain('text-muted')
  })

  it('className 可被调用方覆盖/追加', () => {
    const cls = render({ minutes: 15, className: 'ml-2' })?.className ?? ''
    expect(cls).toContain('ml-2')
    expect(cls).toContain('text-muted')
  })
})
