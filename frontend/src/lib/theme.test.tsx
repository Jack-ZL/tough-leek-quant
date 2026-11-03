// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

let host: HTMLDivElement
let root: Root | undefined
let remote: Record<string, unknown>
let read: () => Promise<Response>

/**
 * 内存版 localStorage — Node 22+ 自带一个需要 --localstorage-file 才可用的
 * localStorage 全局, 会遮住 jsdom 的那个, 本环境下直接访问是 undefined。
 * 与 theme.test.ts 里的同名辅助保持一致口径。
 */
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

/**
 * jsdom 26 不实现 matchMedia, 而融合后的 useTheme 要靠它跟随系统暗色偏好。
 * 这里固定 matches=true (系统偏好暗色): auto 模式下解析结果就是 dark, 与本项目
 * 历史上的默认外观一致, 也让"默认 dark"这类断言在 auto 语义下继续成立。
 */
function stubMatchMedia() {
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    matches: true,
    media: '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    onchange: null,
    dispatchEvent: vi.fn(),
  })))
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetModules()
  vi.useFakeTimers()
  vi.stubGlobal('localStorage', makeLocalStorage())
  stubMatchMedia()
  localStorage.clear()
  document.documentElement.classList.add('dark')
  remote = {}
  read = async () => Response.json({ ui_prefs: remote })
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      Object.assign(remote, JSON.parse(init.body as string))
      return Response.json({ ui_prefs: remote })
    }
    return read()
  }))
  host = document.createElement('div')
  document.body.appendChild(host)
})

afterEach(async () => {
  if (root) await act(async () => root!.unmount())
  root = undefined
  host.remove()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function renderTheme() {
  const theme = await import('./theme')
  function Probe() { return <output>{theme.useTheme()}</output> }
  root = createRoot(host)
  await act(async () => root!.render(<Probe />))
  return theme
}

async function restore() {
  const { hydrateUiPrefs } = await import('./uiPrefs')
  const { applyTheme, getTheme } = await import('./theme')
  await act(async () => { await hydrateUiPrefs(); applyTheme(getTheme()) })
}

it('persists a theme toggle and immediately updates the page and subscribers', async () => {
  const { setTheme } = await renderTheme()
  await act(async () => setTheme('light'))
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  expect(localStorage.getItem('tf-theme')).toBe('light')
  expect(document.documentElement.classList.contains('dark')).toBe(false)
  expect(host.textContent).toBe('light')
  expect(remote.theme).toBe('light')
})

it.each(['light', 'dark'] as const)('restores %s on a new origin and updates mounted charts through useTheme', async mode => {
  remote.theme = mode
  if (mode === 'dark') document.documentElement.classList.remove('dark')
  await renderTheme()
  await restore()
  expect(localStorage.getItem('tf-theme')).toBe(mode)
  expect(document.documentElement.classList.contains('dark')).toBe(mode === 'dark')
  expect(host.textContent).toBe(mode)
})

it('uses the saved theme instead of a stale cache from a previous port', async () => {
  remote.theme = 'light'
  localStorage.setItem('tf-theme', 'dark')
  await renderTheme()
  await restore()
  expect(host.textContent).toBe('light')
  expect(remote.theme).toBe('light')
})

it('preserves a user toggle made during slow startup restoration', async () => {
  let resolve!: (value: Response) => void
  read = () => new Promise<Response>(done => { resolve = done })
  const { setTheme } = await renderTheme()
  const { hydrateUiPrefs } = await import('./uiPrefs')
  const restoring = hydrateUiPrefs()
  await act(async () => setTheme('light'))
  await act(async () => { resolve(Response.json({ ui_prefs: { theme: 'dark' } })); await restoring })
  await restore()
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  expect(host.textContent).toBe('light')
  expect(remote.theme).toBe('light')
})

it('migrates an existing local theme when no backend preference exists', async () => {
  localStorage.setItem('tf-theme', 'light')
  await renderTheme()
  await restore()
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  expect(host.textContent).toBe('light')
  expect(remote.theme).toBe('light')
})

it('keeps the default dark theme without overwriting an unset preference', async () => {
  await renderTheme()
  await restore()
  await act(async () => { await vi.advanceTimersByTimeAsync(300) })
  expect(host.textContent).toBe('dark')
  expect(remote).toEqual({})
})

it('applies a theme changed from another tab to both CSS and subscribers', async () => {
  await renderTheme()
  await act(async () => {
    localStorage.setItem('tf-theme', 'light')
    window.dispatchEvent(new StorageEvent('storage', { key: 'tf-theme', newValue: 'light' }))
  })
  expect(host.textContent).toBe('light')
  expect(document.documentElement.classList.contains('dark')).toBe(false)
})

it('restores the saved theme after an unauthorized startup request and SPA login', async () => {
  const { hydrateUiPrefs } = await import('./uiPrefs')
  read = async () => new Response(null, { status: 401 })
  await hydrateUiPrefs()
  remote.theme = 'light'
  read = async () => Response.json({ ui_prefs: remote })
  await renderTheme()
  expect(host.textContent).toBe('light')
  expect(document.documentElement.classList.contains('dark')).toBe(false)
})

it('keeps a theme changed by another tab while hydration was pending', async () => {
  localStorage.setItem('tf-theme', 'dark')
  let resolve!: (value: Response) => void
  read = () => new Promise<Response>(done => { resolve = done })
  await renderTheme()
  await act(async () => {
    localStorage.setItem('tf-theme', 'light')
    window.dispatchEvent(new StorageEvent('storage', { key: 'tf-theme', newValue: 'light' }))
  })
  await act(async () => resolve(Response.json({ ui_prefs: { theme: 'dark' } })))
  expect(host.textContent).toBe('light')
  expect(document.documentElement.classList.contains('dark')).toBe(false)
})
