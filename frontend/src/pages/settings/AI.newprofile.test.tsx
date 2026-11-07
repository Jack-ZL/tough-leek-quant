// @vitest-environment jsdom
// 回归: 「新建」模型档案保存时必须只入库 (POST /ai/profiles, apply=false),
// 绝不能走 saveAiSettings 把当前在用的模型顶掉; 编辑已有档案才走生效保存。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsAIPanel } from './AI'

const mocks = vi.hoisted(() => ({
	settings: vi.fn(), preferences: vi.fn(), getAiProfiles: vi.fn(), aiKeyHealth: vi.fn(),
	debateRoleProfilesGet: vi.fn(), getModelCapabilities: vi.fn(),
	saveAiProfile: vi.fn(), saveAiSettings: vi.fn(), activateAiProfile: vi.fn(),
	deleteAiProfile: vi.fn(), testAiSettings: vi.fn(), updateAiNumberGate: vi.fn(),
	clearAiSettings: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ api: mocks }))

const qwenProfile = {
	id: 'qwen3-8-max', name: 'qwen3.8-max', provider: 'openai_compat',
	protocol: 'chat_completions', base_url: 'https://relay/v1', api_key: 'sk-q',
	model: 'qwen3.8-max', user_agent: '', max_output_tokens: 16384,
	context_window: 128000, web_search: true, reasoning_effort: 'high',
	codex_command: '', codex_model: '', codex_reasoning_effort: '',
}

let root: Root
let qc: QueryClient
const node = document.createElement('div')
document.body.appendChild(node)

const button = (text: string) =>
	[...node.querySelectorAll('button')].find(el => el.textContent?.trim().includes(text))!
const input = (placeholder: string) =>
	node.querySelector<HTMLInputElement>(`input[placeholder="${placeholder}"]`)!
async function click(el: HTMLElement) {
	expect(el).toBeTruthy()
	await act(async () => el.click())
}
async function type(el: HTMLInputElement, value: string) {
	const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
	await act(async () => {
		setter.call(el, value)
		el.dispatchEvent(new Event('input', { bubbles: true }))
	})
}
async function render() {
	qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
	root = createRoot(node)
	await act(async () => root.render(
		<QueryClientProvider client={qc}><SettingsAIPanel /></QueryClientProvider>,
	))
}

beforeEach(() => {
	Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
	Object.values(mocks).forEach(fn => fn.mockReset())
	mocks.settings.mockResolvedValue({
		mode: 'api_key', has_ai_key: true, ai_configured: true,
		ai_provider: 'openai_compat', ai_base_url: 'https://relay/v1', ai_model: 'qwen3.8-max',
		ai_api_key_masked: 'sk-s......XyMo', onboarding_completed: true,
	})
	mocks.preferences.mockResolvedValue({})
	mocks.getAiProfiles.mockResolvedValue({ profiles: [qwenProfile], active: 'qwen3-8-max' })
	mocks.aiKeyHealth.mockResolvedValue({ events: [] })
	mocks.debateRoleProfilesGet.mockResolvedValue({ profiles: [], active_profile_id: '' })
	mocks.getModelCapabilities.mockResolvedValue({ registered: false })
	mocks.updateAiNumberGate.mockResolvedValue({ ok: true })
	mocks.saveAiProfile.mockResolvedValue({
		ok: true, saved_id: 'glm', profiles: [{ ...qwenProfile }, { id: 'glm', name: 'GLM', model: 'glm-4.7' }],
		ai_active_profile: 'qwen3-8-max', ai_model: 'qwen3.8-max',
	})
	mocks.saveAiSettings.mockResolvedValue({
		ok: true, ai_active_profile: 'qwen3-8-max', ai_model: 'qwen3.8-max',
		ai_profiles: [{ id: 'qwen3-8-max', name: 'qwen3.8-max', model: 'qwen3.8-max' }],
	})
	mocks.activateAiProfile.mockResolvedValue({ ok: true, ai_active_profile: 'qwen3-8-max' })
	mocks.deleteAiProfile.mockResolvedValue({ ok: true, profiles: [], active: '' })
	mocks.testAiSettings.mockResolvedValue({ ok: true, model: 'm', response: '' })
	mocks.clearAiSettings.mockResolvedValue({ ok: true })
})
afterEach(() => {
	act(() => root.unmount())
	node.innerHTML = ''
})

it('新建保存只入库: 走档案端点 apply=false, 不碰生效配置', async () => {
	await render()
	await click(button('新建'))
	// 新建态有在用的模型时, 按钮文案与提示都要说清楚「只入库」
	expect(button('存入档案库')).toBeTruthy()
	expect(node.textContent).toContain('新建保存只存入档案库')
	await type(input('https://llm.runninghub.ai/v1'), 'https://token-plan/v1')
	await type(input('gpt-5.6-sol'), 'glm-4.7')
	await type(input('如: 千问-max'), 'GLM')
	await click(button('存入档案库'))
	expect(mocks.saveAiProfile).toHaveBeenCalledTimes(1)
	const arg = mocks.saveAiProfile.mock.calls[0][0]
	expect(arg).toMatchObject({ name: 'GLM', model: 'glm-4.7', base_url: 'https://token-plan/v1', apply: false })
	// 关键回归: 生效配置保存端点一次都不能被调用
	expect(mocks.saveAiSettings).not.toHaveBeenCalled()
})

it('编辑已有档案保存仍走生效配置端点并带 profile_id', async () => {
	await render()
	// 挂载后编辑目标=激活档案; 点档案行回显再保存
	await click(node.querySelector<HTMLElement>('button[title="载入此档案的配置到右侧表单"]')!)
	await click(button('保存配置'))
	expect(mocks.saveAiSettings).toHaveBeenCalledTimes(1)
	expect(mocks.saveAiSettings.mock.calls[0][0]).toMatchObject({ profile_id: 'qwen3-8-max' })
	expect(mocks.saveAiProfile).not.toHaveBeenCalled()
})

it('档案库为空时首启保存仍走生效配置端点 (new_profile 追加)', async () => {
	mocks.getAiProfiles.mockResolvedValue({ profiles: [], active: '' })
	await render()
	await type(input('https://llm.runninghub.ai/v1'), 'https://relay/v1')
	await type(input('gpt-5.6-sol'), 'qwen3.8-max')
	await click(button('保存配置'))
	expect(mocks.saveAiSettings).toHaveBeenCalledTimes(1)
	expect(mocks.saveAiSettings.mock.calls[0][0]).toMatchObject({ new_profile: true })
	expect(mocks.saveAiProfile).not.toHaveBeenCalled()
})
