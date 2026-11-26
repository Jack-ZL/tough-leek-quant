// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_MARKET, MARKETS } from './market'
import {
  MARKET_SWITCHER_SCOPE_HINT,
  MARKET_SWITCHER_SCOPE_HINT_CN,
  getMarket,
  marketSwitcherScopeHint,
  setMarket,
  subscribeMarket,
  useMarket,
} from './marketStore'

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  // 🔴 模块级状态跨用例残留, 必须显式复位 (与 Indices.market.test.tsx 同因)。
  setMarket(DEFAULT_MARKET)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

describe('初值与持久化', () => {
  it('无存储时取缺省市场', () => {
    expect(getMarket()).toBe(DEFAULT_MARKET)
  })

  it('setMarket 写 localStorage 并更新内存态', () => {
    setMarket('HK')
    expect(getMarket()).toBe('HK')
    expect(localStorage.getItem('tf-market')).toBe('HK')
  })

  it('同值 setMarket 是 no-op, 不通知订阅者', () => {
    let calls = 0
    const off = subscribeMarket(() => { calls += 1 })
    try {
      setMarket(getMarket())
      expect(calls).toBe(0)
    } finally {
      off()
    }
  })
})

describe('🔴 非法存储值必须回落缺省市场, 而不是被「规整」成合法市场', () => {
  // lib/market.ts 的分流铁律: 这里读到的是**用户上次的明确选择**, 落库值非法时
  // 若用 normalizeMarket 兜底, 任意脏字符串都会被洗成一个看似合法的市场, 再被拿去
  // 拼请求参数 —— 那正是「把港股代码当 A 股查, 静默返回空」的入口。
  //
  // 初值只在模块加载时读一次, 所以每个用例都要 resetModules 拿一个干净实例。
  async function freshStore(stored: string | null) {
    localStorage.clear()
    if (stored !== null) localStorage.setItem('tf-market', stored)
    vi.resetModules()
    return await import('./marketStore')
  }

  it.each(['hk', 'HK ', 'US', 'CN\n', '', 'null', '港股', 'cn'])('存储值 %j 不被接受', async (bad) => {
    const mod = await freshStore(bad)
    expect(mod.getMarket()).toBe(DEFAULT_MARKET)
  })

  it.each(MARKETS)('合法存储值 %s 被原样接受', async (m) => {
    const mod = await freshStore(m)
    expect(mod.getMarket()).toBe(m)
  })

  it('无存储时取缺省市场', async () => {
    const mod = await freshStore(null)
    expect(mod.getMarket()).toBe(DEFAULT_MARKET)
  })

  afterEach(() => {
    // resetModules 会让后续用例拿到新实例; 顶层 import 的实例已被换掉,
    // 这里复位 localStorage, 避免脏值渗到别的 describe。
    localStorage.clear()
    vi.resetModules()
  })
})

describe('订阅与跨标签页同步', () => {
  it('setMarket 通知订阅者, 退订后不再通知', () => {
    let calls = 0
    const off = subscribeMarket(() => { calls += 1 })
    setMarket('HK')
    expect(calls).toBe(1)
    off()
    setMarket(DEFAULT_MARKET)
    expect(calls).toBe(1)
  })

  it('另一个标签页写 localStorage 会同步本标签页内存态', () => {
    let calls = 0
    const off = subscribeMarket(() => { calls += 1 })
    try {
      localStorage.setItem('tf-market', 'HK')
      window.dispatchEvent(new StorageEvent('storage', { key: 'tf-market' }))
      expect(getMarket()).toBe('HK')
      expect(calls).toBe(1)
    } finally {
      off()
    }
  })

  it('别的 key 的 storage 事件不改市场', () => {
    localStorage.setItem('tf-market', 'HK')
    window.dispatchEvent(new StorageEvent('storage', { key: 'tf-theme' }))
    // 上一行 setItem 未经 setMarket, 内存态仍是缺省; 无关事件不得把它拉成 HK
    expect(getMarket()).toBe(DEFAULT_MARKET)
  })

  it('storage 事件里的非法值同样回落缺省市场', () => {
    setMarket('HK')
    localStorage.setItem('tf-market', 'US')
    window.dispatchEvent(new StorageEvent('storage', { key: 'tf-market' }))
    expect(getMarket()).toBe(DEFAULT_MARKET)
  })
})

describe('useMarket hook', () => {
  function Probe({ onRender }: { onRender: (m: string) => void }) {
    const m = useMarket()
    onRender(m)
    return <span data-market>{m}</span>
  }

  it('切换市场触发重渲染', async () => {
    const seen: string[] = []
    await act(async () => root.render(<Probe onRender={m => seen.push(m)} />))
    expect(host.querySelector('[data-market]')!.textContent).toBe(DEFAULT_MARKET)
    await act(async () => setMarket('HK'))
    expect(host.querySelector('[data-market]')!.textContent).toBe('HK')
    expect(seen).toContain('HK')
  })
})

describe('🔴 作用范围说明文案 (不误导的唯一防线)', () => {
  // 切换器挂在全局位置, 用户会默认它切整个 App; 而 8 个核心导航项里只有「指数」
  // 对港股就绪。删掉或改弱这句文案 = 让用户在看板/自选页看到 A 股数据却以为是港股。
  it('明写只有指数与个股详情支持港股', () => {
    expect(MARKET_SWITCHER_SCOPE_HINT).toContain('仅指数')
    expect(MARKET_SWITCHER_SCOPE_HINT).toContain('个股详情')
    expect(MARKET_SWITCHER_SCOPE_HINT).toContain('A 股')
  })

  it('不是空壳措辞 (防止被改成「切换市场」这类无范围文案)', () => {
    expect(MARKET_SWITCHER_SCOPE_HINT.length).toBeGreaterThan(12)
    expect(MARKET_SWITCHER_SCOPE_HINT).not.toBe('切换市场')
  })

  it('A 股专属文案: helper 按市场返回各自措辞', () => {
    expect(marketSwitcherScopeHint('CN')).toBe(MARKET_SWITCHER_SCOPE_HINT_CN)
    expect(marketSwitcherScopeHint('HK')).toBe(MARKET_SWITCHER_SCOPE_HINT)
    expect(MARKET_SWITCHER_SCOPE_HINT_CN).toContain('A 股')
    expect(MARKET_SWITCHER_SCOPE_HINT_CN).not.toBe(MARKET_SWITCHER_SCOPE_HINT)
  })
})
