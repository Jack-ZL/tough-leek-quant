// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { SettingsState } from '@/lib/api'
import { SettingsAIPanel } from './AI'

const mocks = vi.hoisted(() => ({ settings: { data: {} as Partial<SettingsState> } }))
vi.mock('@/lib/useSharedQueries', () => ({ useSettings: () => mocks.settings, usePreferences: () => ({ data: {} }) }))
vi.mock('@/lib/sponsor-discount', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/sponsor-discount')>(),
  useSponsorModelItems: () => null,
}))

let root: Root
let client: QueryClient
let host: HTMLDivElement

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  mocks.settings.data = {
    ai_provider: 'codex_cli', ai_configured: true, ai_codex_command: 'codex',
    ai_codex_model: '', ai_codex_reasoning_effort: '',
  }
})

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

// 上游用「预设卡片网格 + aria-pressed」渲染服务商选择器; 本仓库已改成下拉 (触发按钮
// aria-haspopup="listbox", 选项 role="option" + aria-selected), 见 AI.tsx「服务商下拉」。
// 下面两个 helper 按下拉的可达性语义取同一组预设, 让上游用例的断言意图 (预设顺序、
// 优惠徽标、选中态、切换后的表单联动) 原样保留。
async function openProviderList() {
  const trigger = host.querySelector<HTMLButtonElement>('button[aria-haspopup="listbox"]')
  expect(trigger).toBeTruthy()
  await act(async () => trigger!.click())
}

function providerOptions() {
  return [...host.querySelectorAll<HTMLButtonElement>('[role="option"]')]
}

// 本仓库挂载时还会拉模型档案库 (/api/settings/ai/profiles) 与密钥健康
// (/api/settings/ai/key-health) —— 上游没有这两个端点, 会让「fetch 被调用 N 次」的
// 计数失真。这里只统计用例真正要断言的两个端点: 生效配置保存与连通性测试。
function aiCalls(fetcher: { mock: { calls: unknown[][] } }) {
  return fetcher.mock.calls.filter(([url]) => {
    const u = String(url)
    return u === '/api/settings/ai' || u === '/api/settings/ai/test'
  })
}

function bodyOf(init: unknown) {
  return JSON.parse((init as RequestInit).body as string)
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

it('edits a saved OpenAI configuration under Custom without an OpenAI preset card', async () => {
  mocks.settings.data = {
    ai_provider: 'openai',
    ai_protocol: 'responses',
    ai_base_url: 'https://api.openai.com/v1',
    ai_model: 'saved-openai-model',
    ai_reasoning_effort: 'xhigh',
    has_ai_key: true,
    ai_configured: true,
  }
  // 每次调用都要给一个新的 Response: body 只能读一次, 而挂载时的档案库/密钥健康也会
  // 打到同一个 stub 上。
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ ok: true })))
  vi.stubGlobal('fetch', fetcher)
  await setup()

  await openProviderList()
  const options = providerOptions()
  expect(options.some(option => option.textContent?.startsWith('OpenAI'))).toBe(false)
  const [custom, modelflare] = options
  expect(custom.textContent).toContain('自定义')
  expect(custom.getAttribute('aria-selected')).toBe('true')
  await act(async () => modelflare.click())
  // 选中即收起下拉, 选项节点随之卸载 ⇒ 换回「自定义」要重新展开并重新取引用。
  await openProviderList()
  await act(async () => providerOptions()[0].click())

  await click('保存配置')
  const saves = aiCalls(fetcher)
  expect(saves).toHaveLength(1)
  const [url, init] = saves[0]
  expect(url).toBe('/api/settings/ai')
  expect(bodyOf(init)).toMatchObject({
    provider: 'openai', protocol: 'responses', base_url: 'https://api.openai.com/v1',
    model: 'saved-openai-model', reasoning_effort: 'xhigh',
  })
  expect(bodyOf(init)).not.toHaveProperty('api_key')
})

it('places Modelflare after Custom and tests its Chat Completions preset with its own key', async () => {
  mocks.settings.data = {
    ai_provider: 'openai_compat',
    ai_protocol: 'responses',
    ai_base_url: 'https://llm.runninghub.ai/v1',
    ai_model: 'saved-model',
    has_ai_key: true,
    ai_configured: true,
  }
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ ok: true, model: 'gpt-5.6-sol' })))
  vi.stubGlobal('fetch', fetcher)
  await setup()

  await openProviderList()
  const [custom, modelflare, runningHub] = providerOptions()
  expect(custom.textContent).toContain('自定义')
  expect(modelflare.textContent).toContain('Modelflare')
  expect(modelflare.textContent).toContain('优惠')
  expect(runningHub.textContent).toContain('RunningHub')
  expect(runningHub.getAttribute('aria-selected')).toBe('true')
  await act(async () => modelflare.click())

  expect(host.textContent).toContain('US$20 解锁 GPT 0.015x')
  expect(host.textContent).toContain('US$50 解锁 Claude 0.03x')
  expect(host.textContent).toContain('65% / 75% / 85%')
  expect(host.querySelector('code')?.textContent).toBe('f1cc942c2f674f568106bb244f0f1ac0')
  expect(host.textContent).toContain('每位用户限用一次')
  expect(host.querySelector('[aria-label="获取模型列表"]')).toBeNull()
  expect(host.querySelector<HTMLInputElement>('input[placeholder^="https://"]')?.value).toBe('https://modelflare.dev/v1')
  expect(host.querySelector<HTMLInputElement>('input[placeholder="gpt-5.6-sol"]')?.value).toBe('gpt-5.6-sol')
  expect([...host.querySelectorAll('button[aria-pressed="true"]')].some(
    button => button.textContent === 'Chat Completions',
  )).toBe(true)
  await click('测试')
  await click('测试连接')
  await click('保存配置')
  // 未填 Key 时测试按钮不可用、保存也被守卫拦住 ⇒ 这两个端点一次都不该打到。
  expect(aiCalls(fetcher)).toHaveLength(0)

  const keyInput = host.querySelector<HTMLInputElement>('input[type="password"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(keyInput, 'test-modelflare-key')
    keyInput.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click('测试连接')

  const tested = aiCalls(fetcher)
  expect(tested).toHaveLength(1)
  const [url, init] = tested[0]
  expect(url).toBe('/api/settings/ai/test')
  expect(bodyOf(init)).toEqual({
    provider: 'openai_compat', protocol: 'chat_completions', base_url: 'https://modelflare.dev/v1',
    model: 'gpt-5.6-sol', api_key: 'test-modelflare-key',
  })
  expect(host.querySelector('[role=status]')?.textContent).toContain('连通成功')

  // runningHub 节点已随下拉收起卸载, 重新展开后按同一序号 (第 3 个预设) 取回。
  await openProviderList()
  await act(async () => providerOptions()[2].click())
  expect(host.querySelector('[aria-label="获取模型列表"]')).not.toBeNull()
  expect(keyInput.value).toBe('')
  expect([...host.querySelectorAll('button')].find(button => button.textContent === '测试连接')?.disabled).toBe(false)
  expect(aiCalls(fetcher)).toHaveLength(1)
})
