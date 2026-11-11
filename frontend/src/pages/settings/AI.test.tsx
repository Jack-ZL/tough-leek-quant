// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsAIPanel } from './AI'

const mocks = vi.hoisted(() => ({ settings: { data: {
  ai_provider: 'codex_cli', ai_configured: true, ai_codex_command: 'codex',
  ai_codex_model: '', ai_codex_reasoning_effort: '',
} } }))
vi.mock('@/lib/useSharedQueries', () => ({ useSettings: () => mocks.settings, usePreferences: () => ({ data: {} }) }))
vi.mock('@/lib/sponsor-discount', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/sponsor-discount')>(),
  useSponsorModelItems: () => null,
}))

let root: Root
let client: QueryClient
let host: HTMLDivElement

beforeEach(() => vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true))

afterEach(() => {
  act(() => root?.unmount())
  client?.clear()
  host?.remove()
  vi.unstubAllGlobals()
})

async function setup() {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => root.render(
    <QueryClientProvider client={client}><MemoryRouter><SettingsAIPanel /></MemoryRouter></QueryClientProvider>,
  ))
}

async function click(label: string) {
  const button = [...host.querySelectorAll('button')].find(el => el.textContent?.trim() === label)
  expect(button).toBeTruthy()
  await act(async () => button!.click())
}

it('both test buttons show the complete routing error and clear it after a successful retry', async () => {
  // dev 侧面板挂载时还会拉档案/密钥健康等接口 — 按 URL 路由, 只给 ai/test 排队列响应
  const testResponses = [
    new Response(JSON.stringify({ detail: 'Method Not Allowed' }), { status: 405 }),
    new Response(JSON.stringify({ ok: true, model: 'codex_cli', response: 'OK' })),
  ]
  const fetcher = vi.fn((url: unknown, _init?: RequestInit) => Promise.resolve(
    String(url).endsWith('/api/settings/ai/test') ? testResponses.shift()! : new Response('{}'),
  ))
  vi.stubGlobal('fetch', fetcher)
  await setup()
  await click('测试连接')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('前后端版本一致')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('POST /api/settings/ai/test')
  expect(host.querySelector('[role=status]')?.textContent).toContain('测试失败')
  await click('测试')
  expect(host.querySelector('[role=alert]')).toBeNull()
  expect(host.querySelector('[role=status]')?.textContent).toContain('连通成功')
  const testCalls = fetcher.mock.calls.filter(([url]) => String(url).endsWith('/api/settings/ai/test'))
  expect(testCalls).toHaveLength(2)
  for (const [url, init] of testCalls) {
    expect(url).toBe('/api/settings/ai/test')
    expect(JSON.parse(String(init?.body))).toEqual({ provider: 'codex_cli', model: '' })
  }
})

it('shows the full CLI login guidance without replacing it with a deployment error', async () => {
  const error = 'Codex CLI 调用失败: 401 Unauthorized\n请执行 codex login'
  vi.stubGlobal('fetch', vi.fn((url: unknown) => Promise.resolve(
    String(url).endsWith('/api/settings/ai/test')
      ? new Response(JSON.stringify({ ok: false, error }))
      : new Response('{}'),
  )))
  await setup()
  await click('测试')
  expect(host.querySelector('[role=alert]')?.textContent).toBe(error)
})
