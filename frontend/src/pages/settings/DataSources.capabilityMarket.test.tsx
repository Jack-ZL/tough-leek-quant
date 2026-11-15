// @vitest-environment jsdom
/**
 * 能力卡的 market 维渲染 (方案 §6)。
 *
 * 钉住三件事:
 *  1. market_supported=false → 只渲染「X 不支持 + 原因」, **不渲染**路由 UI
 *     (不显示假数据、不给用户一个点了也没用的下拉)。
 *  2. 直连市场 (routed=false) 的行 current/effective 可能为 null, 卡片不得崩。
 *  3. CN 行行为零变化 —— 设置页现有调用不传 market, 后端缺省 CN。
 *
 * 判定全部来自后端字段, 前端不硬编码任何能力名单; 所以这里用「假想能力 id」
 * 也能过, 反证组件没有偷偷按 id 分支。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { CapabilityRoute } from '@/lib/api'
import { CapabilityCard } from './DataSources'

let root: Root | undefined
let host: HTMLDivElement

function row(over: Partial<CapabilityRoute> = {}): CapabilityRoute {
  return {
    id: 'daily',
    label: '日K',
    desc: '日线行情',
    field: 'daily_data_provider',
    default: 'tickflow',
    tf_tier: 'free',
    tf_available: true,
    usable: true,
    current: 'tickflow',
    current_display: 'TickFlow',
    effective: 'tickflow',
    effective_display: 'TickFlow',
    candidates: [{ name: 'tickflow', display: 'TickFlow', kind: 'builtin', available: true, status: 'ok' }],
    pending: [],
    complement: null,
    complement_display: null,
    market: 'CN',
    routed: true,
    market_supported: true,
    market_source: null,
    market_reason: null,
    market_note: null,
    ...over,
  }
}

/** 港股直连行的真实形状: 无偏好字段、无候选、无档位。 */
function hkRow(over: Partial<CapabilityRoute> = {}): CapabilityRoute {
  return row({
    market: 'HK',
    routed: false,
    field: null,
    current: null,
    current_display: null,
    candidates: [],
    pending: [],
    tf_available: false,
    complement: null,
    complement_display: null,
    ...over,
  })
}

function render(cap: CapabilityRoute, onSelect = vi.fn()) {
  act(() => {
    root = createRoot(host)
    root.render(<CapabilityCard cap={cap} pendingKey={null} onSelect={onSelect} />)
  })
  return onSelect
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
})

afterEach(() => {
  act(() => root?.unmount())
  root = undefined
  host.remove()
})

const text = () => host.textContent ?? ''

it('不支持的能力: 渲染市场与原因, 不渲染路由 UI', () => {
  render(hkRow({
    id: 'depth5',
    label: '五档盘口',
    usable: false,
    market_supported: false,
    effective: null,
    effective_display: null,
    market_reason: '港股无五档盘口源; 且连板/涨停是 A 股独有语义',
  }))
  expect(text()).toContain('五档盘口')
  expect(text()).toContain('HK 不支持')
  expect(text()).toContain('港股无五档盘口源')
  // 🔴 不渲染路由区: 没有「当前」标签、没有候选芯片、没有「暂无可用提供方」
  expect(text()).not.toContain('当前')
  expect(text()).not.toContain('暂无可用提供方')
  expect(host.querySelectorAll('button')).toHaveLength(0)
})

it('不支持且原因为空: 回退到通用文案而不是渲染空白', () => {
  render(hkRow({ id: 'minute', label: '分钟K', usable: false, market_supported: false, market_reason: null }))
  expect(text()).toContain('该市场未提供此能力')
})

it('支持但带口径备注: 备注可见 (港股复权是加性偏移这类坑必须显式说明)', () => {
  render(hkRow({
    id: 'adj_factor',
    label: '复权因子',
    effective: 'eastmoney',
    effective_display: 'eastmoney',
    market_source: 'eastmoney',
    market_note: 'F10 分红事件的加性偏移(非乘性)',
  }))
  expect(text()).toContain('复权因子')
  expect(text()).toContain('F10 分红事件的加性偏移')
  expect(text()).not.toContain('不支持')
})

it('港股直连行 (current/effective 为 null) 不崩, 且不给可点的路由按钮', () => {
  render(hkRow({ id: 'daily', label: '日K', effective: 'eastmoney', effective_display: 'eastmoney', market_source: 'eastmoney' }))
  expect(text()).toContain('日K')
  expect(text()).toContain('eastmoney')
  // candidates 恒空 → 落到「暂无可用提供方」占位, 而不是渲染出可点击的假路由
  expect(text()).toContain('暂无可用提供方')
  expect(host.querySelectorAll('button')).toHaveLength(0)
})

it('CN 行行为不变: 渲染候选芯片, 点击回写偏好字段', () => {
  const onSelect = render(row({
    candidates: [
      { name: 'tickflow', display: 'TickFlow', kind: 'builtin', available: true, status: 'ok' },
      { name: 'fuyao', display: '扶摇', kind: 'custom', available: true, status: 'ok' },
    ],
  }))
  const buttons = [...host.querySelectorAll('button')]
  expect(buttons.map(b => b.textContent)).toEqual(['TickFlow', '扶摇'])
  act(() => { buttons[1].dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  expect(onSelect).toHaveBeenCalledWith('daily_data_provider', 'fuyao')
})

it('CN 行即便 usable=false 也仍渲染路由 UI (市场支持 ≠ 当前源可用, 两者不可混为一谈)', () => {
  render(row({ usable: false, current: 'ghost', current_display: 'ghost', effective: 'ghost', effective_display: 'ghost', candidates: [] }))
  expect(text()).toContain('能力不可用')
  expect(text()).not.toContain('不支持')
  expect(text()).toContain('当前')
})

it('不按能力 id 分支: 未知 id 的市场不支持行同样只渲染原因', () => {
  render(hkRow({
    id: 'some_future_capability',
    label: '未来能力',
    usable: false,
    market_supported: false,
    market_reason: '该市场尚未接入',
  }))
  expect(text()).toContain('HK 不支持')
  expect(text()).toContain('该市场尚未接入')
  expect(host.querySelectorAll('button')).toHaveLength(0)
})
