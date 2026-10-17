// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getTheme, setTheme, toggleTheme } from './theme'

const KEY = 'tf-theme'
const AT_KEY = 'tf-theme-manual-at'
const DAY = 24 * 60 * 60 * 1000

let systemDark = false

/** 内存版 localStorage — 本环境 (Node 25) 未提供 --localstorage-file, jsdom 下 global/localStorage 均为 undefined。 */
function makeLocalStorage() {
  const store = new Map<string, string>()
  return {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => { store.set(k, v) },
    removeItem: (k: string) => { store.delete(k) },
    clear: () => { store.clear() },
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() { return store.size },
  }
}

beforeEach(() => {
  vi.stubGlobal('localStorage', makeLocalStorage())
  systemDark = false
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    get matches() { return systemDark },
    media: '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    onchange: null,
    dispatchEvent: vi.fn(),
  })))
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('theme', () => {
  it('默认跟随系统偏好 (auto)', () => {
    systemDark = true
    expect(getTheme()).toBe('dark')
    systemDark = false
    expect(getTheme()).toBe('light')
  })

  it('手动切换后覆盖系统偏好并记录时间戳', () => {
    systemDark = true
    setTheme('light')
    expect(getTheme()).toBe('light')
    expect(localStorage.getItem(KEY)).toBe('light')
    expect(Number(localStorage.getItem(AT_KEY))).toBe(Date.now())
  })

  it('24 小时内保持手动覆盖', () => {
    systemDark = false
    setTheme('dark')
    vi.advanceTimersByTime(DAY - 1)
    expect(getTheme()).toBe('dark')
  })

  it('24 小时后恢复自动切换 (回落系统偏好)', () => {
    systemDark = true
    setTheme('light')
    expect(getTheme()).toBe('light')
    vi.advanceTimersByTime(DAY)
    expect(getTheme()).toBe('dark')
  })

  it('toggleTheme 在 auto 下切换到系统主题的反面', () => {
    systemDark = true
    expect(toggleTheme()).toBe('light')
    expect(getTheme()).toBe('light')
  })

  it('旧版无时间戳的 light 视为已过期, 立即回落 auto', () => {
    systemDark = true
    localStorage.setItem(KEY, 'light') // 旧版只存模式, 无 manual-at
    expect(getTheme()).toBe('dark')
  })
})
