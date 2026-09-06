import { useState, useEffect, useRef, useCallback, createContext, useContext } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Save, Loader2, Check, Wifi, WifiOff, Eye, EyeOff, Shield,
  Shuffle, Plug, Zap, Settings2, ExternalLink, Trash2,
  ChevronDown, Plus, Sparkles,
} from 'lucide-react'
import { useSettings } from '@/lib/useSharedQueries'
import { api, type SettingsState, type AiProfile } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useCardFlash, cardFlashCls } from '@/lib/useCardFlash'

const INPUT_CLS =
  'w-full h-9 px-2.5 rounded-lg bg-base border-0 ring-1 ring-border/30 text-xs font-mono text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow'

const toPositiveInt = (v: string) => {
  const n = parseInt(v, 10)
  return Number.isInteger(n) && n > 0 ? n : undefined
}

const CODEX_PROVIDER = 'codex_cli'
const OPENAI_PROVIDER = 'openai'
const OPENAI_COMPAT_PROVIDER = 'openai_compat'
const CODEX_COMMAND = 'codex'
const DEFAULT_CODEX_MODEL = 'gpt-5.6-sol'
const DEFAULT_CODEX_REASONING_EFFORT = 'xhigh'
const DEFAULT_OPENAI_MODEL = 'gpt-5.5'
const DEFAULT_REASONING_EFFORT = 'high'
const SAVED_CODEX_OPTION_VALUE = '__saved_codex_config__'
const CODEX_REASONING_LABELS: Record<string, string> = {
  high: '高',
  xhigh: '极高',
}

type CodexModelOption = { label: string; value: string; model: string; effort: string; hint: string }

const CODEX_MODEL_OPTIONS: CodexModelOption[] = [
  {
    label: 'GPT-5.6 Sol · 极高（推荐）',
    value: 'gpt-5.6-sol:xhigh',
    model: 'gpt-5.6-sol',
    effort: 'xhigh',
    hint: '旗舰档，适合复杂金融分析与专业任务',
  },
  {
    label: 'GPT-5.6 Terra · 极高',
    value: 'gpt-5.6-terra:xhigh',
    model: 'gpt-5.6-terra',
    effort: 'xhigh',
    hint: '平衡智能、速度与使用成本',
  },
  {
    label: 'GPT-5.6 Luna · 极高',
    value: 'gpt-5.6-luna:xhigh',
    model: 'gpt-5.6-luna',
    effort: 'xhigh',
    hint: '适合成本敏感与高频分析任务',
  },
  {
    label: 'gpt-5.5 · 高',
    value: 'gpt-5.5:high',
    model: 'gpt-5.5',
    effort: 'high',
    hint: '使用 gpt-5.5 + high 推理档',
  },
  {
    label: 'gpt-5.5 · 极高',
    value: 'gpt-5.5:xhigh',
    model: 'gpt-5.5',
    effort: 'xhigh',
    hint: '使用 gpt-5.5 + xhigh 推理档',
  },
  {
    label: '跟随本机 Codex 默认',
    value: '',
    model: '',
    effort: '',
    hint: '使用本机 Codex CLI 配置的默认模型与推理强度',
  },
]

const codexModelLabel = (model?: string, effort?: string) => {
  if (!model && !effort) return '默认模型'
  const modelLabel = model || '默认模型'
  const effortLabel = effort ? CODEX_REASONING_LABELS[effort] ?? effort : ''
  return effortLabel ? `${modelLabel} · ${effortLabel}` : modelLabel
}

type AiPreset = {
  label: string
  provider?: string
  url: string
  model: string
  codexCommand?: string
  website: string
  websiteLabel: string
  description: string
  custom?: boolean
}

const PRESETS: AiPreset[] = [
  {
    label: '自定义',
    url: 'https://api.zhaji.dev/v1',
    model: 'gpt-5.5',
    website: 'https://api.zhaji.dev',
    websiteLabel: 'api.zhaji.dev',
    description: '手动填写 API 地址、模型和密钥，默认已预填常用配置。',
    custom: true,
  },
  {
    label: 'OpenAI',
    provider: OPENAI_PROVIDER,
    url: 'https://api.openai.com/v1',
    model: DEFAULT_OPENAI_MODEL,
    website: 'https://platform.openai.com/',
    websiteLabel: 'platform.openai.com',
    description: 'OpenAI 官方接口，可单独配置模型支持的推理强度。',
  },
  {
    label: 'Codex CLI',
    provider: CODEX_PROVIDER,
    url: '',
    model: DEFAULT_CODEX_MODEL,
    codexCommand: CODEX_COMMAND,
    website: 'https://developers.openai.com/codex/noninteractive',
    websiteLabel: 'codex exec',
    description: '调用本机 Codex CLI 的 codex exec, 适合已登录 ChatGPT/Codex 的本地环境。',
  },
  {
    label: 'Anthropic Claude',
    url: 'https://api.anthropic.com/v1',
    model: 'claude-sonnet-4-20250514',
    website: 'https://console.anthropic.com/',
    websiteLabel: 'console.anthropic.com',
    description: 'Anthropic 官方 OpenAI 兼容接口，Claude Sonnet 4 代码与长文本能力强。',
  },
  {
    label: 'Google Gemini',
    url: 'https://generativelanguage.googleapis.com/v1beta/openai',
    model: 'gemini-2.5-pro',
    website: 'https://aistudio.google.com/',
    websiteLabel: 'aistudio.google.com',
    description: 'Google AI Studio OpenAI 兼容接口，Gemini 2.5 Pro 多模态与推理能力强。',
  },
  {
    label: 'Mistral AI',
    url: 'https://api.mistral.ai/v1',
    model: 'mistral-large-latest',
    website: 'https://console.mistral.ai/',
    websiteLabel: 'console.mistral.ai',
    description: 'Mistral 官方 OpenAI 兼容接口，欧洲头部模型，性价比高。',
  },
  {
    label: 'xAI Grok',
    url: 'https://api.x.ai/v1',
    model: 'grok-3-latest',
    website: 'https://console.x.ai/',
    websiteLabel: 'console.x.ai',
    description: 'xAI 官方 OpenAI 兼容接口，Grok 3 实时数据接入，适合新闻与事件分析。',
  },
  {
    label: 'DeepSeek',
    url: 'https://api.deepseek.com',
    model: 'deepseek-v4-pro',
    website: 'https://www.deepseek.com/',
    websiteLabel: 'deepseek.com',
    description: 'DeepSeek 官方 OpenAI 兼容接口。',
  },
  {
    label: '通义千问',
    url: 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
    model: 'qwen3.8-max',
    website: 'https://tongyi.aliyun.com/',
    websiteLabel: 'tongyi.aliyun.com',
    description: '阿里云 DashScope 兼容模式接口。',
  },
  {
    label: '智谱 GLM',
    url: 'https://open.bigmodel.cn/api/paas/v4/chat/completions',
    model: 'glm-5.2',
    website: 'https://open.bigmodel.cn/',
    websiteLabel: 'open.bigmodel.cn',
    description: '智谱 AI 官方 OpenAI 兼容接口。',
  },
  {
    label: '月之暗面（Moonshot）',
    url: 'https://api.moonshot.cn/v1',
    model: 'kimi-k2.7-code',
    website: 'https://platform.moonshot.cn/',
    websiteLabel: 'platform.moonshot.cn',
    description: '月之暗面 Moonshot 官方 OpenAI 兼容接口，支持超长上下文。',
  },
  {
    label: 'MiniMax',
    url: 'https://api.minimaxi.com/v1/responses',
    model: 'MiniMax-M2.7',
    website: 'https://www.minimaxi.com/',
    websiteLabel: 'https://www.minimaxi.com/',
    description: 'MiniMax OpenAI Responses API 兼容的主接口调用MiniMax 模型，生成模型回复，支持流式与非流式。',
  },
  {
    label: '腾讯混元',
    url: 'https://hunyuan.tencentcloudapi.com',
    model: 'Hy3',
    website: 'https://hunyuan.tencent.com/model/hy-model',
    websiteLabel: 'https://hunyuan.tencent.com/model/hy-model',
    description: 'Hy4 preview，770B 紧凑旗舰，1M 上下文，懂规划，能执行，可靠交付',
  },
]

const findPreset = (provider: string, baseUrl: string, codexCommand: string) => PRESETS.find(p => {
  if (p.custom || (p.provider ?? OPENAI_COMPAT_PROVIDER) !== provider) return false
  if (provider === OPENAI_PROVIDER) return true
  return provider === CODEX_PROVIDER ? p.codexCommand === codexCommand : p.url === baseUrl
}) ?? PRESETS[0]

function PresetDropdown({ value, onChange, options }: {
  value: string
  onChange: (v: string) => void
  options: { label: string; value: string }[]
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const selected = options.find(o => o.value === value) ?? options[0]

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex h-9 w-full items-center justify-between rounded-btn border border-border bg-base px-3 text-xs text-foreground hover:border-accent/40 transition-colors"
      >
        <span>{selected.label}</span>
        <ChevronDown className={`h-4 w-4 text-muted transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="absolute z-50 mt-1 w-full rounded-btn border border-border bg-base shadow-lg py-1">
          {options.map(o => (
            <button
              key={o.value}
              type="button"
              onClick={() => {
                onChange(o.value)
                setOpen(false)
              }}
              className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors ${o.value === value ? 'bg-accent/10 text-accent' : 'text-foreground hover:bg-accent/5'}`}
            >
              {o.value === value && <Check className="h-3.5 w-3.5 shrink-0" />}
              <span className={o.value === value ? '' : 'pl-5'}>{o.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function ModelListItem({ profile, isActive, onSelect, onDelete }: {
  profile: AiProfile
  isActive: boolean
  onSelect: () => void
  onDelete: () => void
}) {
  const providerLabel = profile.provider === OPENAI_PROVIDER
    ? 'OpenAI'
    : profile.provider === CODEX_PROVIDER
      ? 'Codex'
      : profile.provider === OPENAI_COMPAT_PROVIDER
        ? '兼容'
        : profile.provider
  const displayName = profile.name || profile.model || profile.base_url || '未命名'

  return (
    <div
      className={`group relative flex items-center gap-3 rounded-lg border px-3 py-2.5 cursor-pointer transition-all ${isActive ? 'border-accent/40 bg-accent/[0.06]' : 'border-border/40 bg-base hover:border-border hover:bg-elevated/30'}`}
      onClick={onSelect}
    >
      {isActive && (
        <span className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 rounded-full bg-accent" />
      )}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <Sparkles className={`h-3.5 w-3.5 shrink-0 ${isActive ? 'text-accent' : 'text-muted/50'}`} />
          <span className={`text-xs font-medium truncate ${isActive ? 'text-foreground' : 'text-secondary'}`}>
            {displayName}
          </span>
          {isActive && (
            <span className="shrink-0 text-[9px] px-1.5 py-0.5 rounded-full bg-accent/10 text-accent font-medium">
              使用中
            </span>
          )}
        </div>
        <div className="text-[10px] text-muted/60 mt-0.5 truncate">
          {providerLabel} · {profile.model || '—'}
        </div>
      </div>
      <button
        onClick={(e) => {
          e.stopPropagation()
          onDelete()
        }}
        className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-danger/10 text-muted hover:text-danger transition-all"
        title="删除此配置"
      >
        <Trash2 className="h-3 w-3" />
      </button>
    </div>
  )
}

export function SettingsAIPanel({ highlight }: { highlight?: string } = {}) {
  const qc = useQueryClient()
  const settings = useSettings()
  const s = settings.data

  const [profiles, setProfiles] = useState<AiProfile[]>(s?.ai_profiles ?? [])
  const [activeProfile, setActiveProfile] = useState<{ name: string; provider: string } | null>(
    s?.ai_model ? { name: s.ai_model, provider: s.ai_provider ?? OPENAI_COMPAT_PROVIDER } : null,
  )

  const [provider, setProvider] = useState(OPENAI_COMPAT_PROVIDER)
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [reasoningEffort, setReasoningEffort] = useState(DEFAULT_REASONING_EFFORT)
  const [codexModel, setCodexModel] = useState('')
  const [codexReasoningEffort, setCodexReasoningEffort] = useState('')
  const [codexCommand, setCodexCommand] = useState(CODEX_COMMAND)
  const [customUa, setCustomUa] = useState(false)
  const [userAgent, setUserAgent] = useState('')
  const [maxOutputTokens, setMaxOutputTokens] = useState('')
  const [contextWindow, setContextWindow] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [saved, setSaved] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<{ name: string; provider: string } | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const [selectedPresetLabel, setSelectedPresetLabel] = useState(PRESETS[0].label)
  const directDrafts = useRef({
    custom: { baseUrl: '', model: '' },
    openai: { baseUrl: 'https://api.openai.com/v1', model: DEFAULT_OPENAI_MODEL },
  })
  const draftsInitialized = useRef(false)

  const isCodexProvider = provider === CODEX_PROVIDER
  const isOpenAIProvider = provider === OPENAI_PROVIDER
  const savedCodexProvider = s?.ai_provider === CODEX_PROVIDER
  const configured = s?.ai_configured ?? (savedCodexProvider ? !!(s?.ai_codex_command ?? CODEX_COMMAND) : s?.has_ai_key)
  const selectedPreset = PRESETS.find(p => p.label === selectedPresetLabel) ?? PRESETS[0]
  const configTitle = isCodexProvider ? 'Codex CLI 配置' : isOpenAIProvider ? 'OpenAI 配置' : selectedPreset.custom ? '自定义配置' : `${selectedPreset.label} 配置`
  const savedCodexModel = s?.ai_codex_model ?? (savedCodexProvider ? (s?.ai_model ?? '') : '')
  const savedCodexEffort = s?.ai_codex_reasoning_effort ?? ''
  const savedCodexOptionKnown = CODEX_MODEL_OPTIONS.some(option =>
    option.model === savedCodexModel && option.effort === savedCodexEffort,
  )
  const savedCodexOption: CodexModelOption | null =
    (savedCodexModel || savedCodexEffort) && !savedCodexOptionKnown
      ? {
          label: `${codexModelLabel(savedCodexModel, savedCodexEffort)}（当前配置）`,
          value: SAVED_CODEX_OPTION_VALUE,
          model: savedCodexModel,
          effort: savedCodexEffort,
          hint: '保留项目中已保存的模型与推理档；此兼容项不可编辑',
        }
      : null
  const codexModelOptions = savedCodexOption
    ? [savedCodexOption, ...CODEX_MODEL_OPTIONS]
    : CODEX_MODEL_OPTIONS
  const selectedCodexModelOption = codexModelOptions.find(option =>
    option.model === codexModel && option.effort === codexReasoningEffort,
  ) ?? CODEX_MODEL_OPTIONS[0]
  const codexModelSelectValue = selectedCodexModelOption.value
  const canSave = isCodexProvider ? true : !!baseUrl.trim() && !!model.trim()

  useEffect(() => {
    if (!s) return
    setProfiles(s.ai_profiles ?? [])
    if (s.ai_model) {
      setActiveProfile({ name: s.ai_model, provider: s.ai_provider ?? OPENAI_COMPAT_PROVIDER })
    }

    const unconfigured = !s.has_ai_key && !s.ai_configured
    const savedProvider = s.ai_provider ?? OPENAI_COMPAT_PROVIDER
    const savedBaseUrl = unconfigured ? '' : (s.ai_base_url ?? '')
    const savedOpenAIModel = unconfigured ? '' : (s.ai_openai_model ?? (savedProvider !== CODEX_PROVIDER ? s.ai_model : '') ?? '')
    const savedPreset = unconfigured ? PRESETS[0] : findPreset(savedProvider, savedBaseUrl, s.ai_codex_command ?? CODEX_COMMAND)
    if (!draftsInitialized.current) {
      const officialOpenAI = PRESETS.find(p => p.provider === OPENAI_PROVIDER)
      if (savedProvider === OPENAI_PROVIDER || (savedProvider === CODEX_PROVIDER && savedBaseUrl === officialOpenAI?.url)) {
        directDrafts.current.openai = { baseUrl: savedBaseUrl, model: savedOpenAIModel }
      } else if (findPreset(OPENAI_COMPAT_PROVIDER, savedBaseUrl, CODEX_COMMAND).custom) {
        directDrafts.current.custom = { baseUrl: savedBaseUrl, model: savedOpenAIModel }
      }
      draftsInitialized.current = true
    }
    setProvider(savedProvider)
    setSelectedPresetLabel(savedPreset.label)
    setBaseUrl(savedBaseUrl)
    setModel(savedOpenAIModel)
    setReasoningEffort(s.ai_reasoning_effort ?? DEFAULT_REASONING_EFFORT)
    setCodexModel(s.ai_codex_model ?? (savedProvider === CODEX_PROVIDER ? s.ai_model : '') ?? '')
    setCodexReasoningEffort(s.ai_codex_reasoning_effort ?? '')
    setCodexCommand(s.ai_codex_command ?? CODEX_COMMAND)
    const ua = s.ai_user_agent ?? ''
    setCustomUa(!!ua)
    setUserAgent(ua)
    setMaxOutputTokens(String(s?.ai_max_output_tokens ?? 8192))
    setContextWindow(String(s?.ai_context_window ?? 64000))
  }, [s])

  const handleSelectProfile = useCallback((profile: AiProfile) => {
    setActiveProfile({ name: profile.name, provider: profile.provider })
    setProvider(profile.provider ?? OPENAI_COMPAT_PROVIDER)
    setBaseUrl(profile.base_url ?? '')
    setModel(profile.model ?? '')
    setReasoningEffort(profile.reasoning_effort ?? DEFAULT_REASONING_EFFORT)
    setCodexModel(profile.provider === CODEX_PROVIDER ? (profile.codex_command ? profile.model : '') : '')
    setCodexReasoningEffort(profile.codex_reasoning_effort ?? '')
    setCodexCommand(profile.codex_command ?? CODEX_COMMAND)
    setUserAgent(profile.user_agent ?? '')
    setCustomUa(!!profile.user_agent)
    setMaxOutputTokens(String(profile.max_output_tokens ?? 8192))
    setContextWindow(String(profile.context_window ?? 64000))
    setApiKey(profile.api_key ?? '')
    setTestResult(null)

    const preset = PRESETS.find(p =>
      (p.provider ?? OPENAI_COMPAT_PROVIDER) === profile.provider
      && (p.url === profile.base_url || p.model === profile.model),
    )
    setSelectedPresetLabel(preset?.label ?? profile.name)
  }, [])

  const activateMutation = useMutation({
    mutationFn: ({ name, provider }: { name: string; provider: string }) =>
      api.activateAiProfile(name, provider),
    onSuccess: (result) => {
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_provider: result.ai_provider ?? prev.ai_provider,
        ai_model: result.ai_model ?? prev.ai_model,
        ai_openai_model: result.ai_openai_model ?? prev.ai_openai_model,
        ai_reasoning_effort: result.ai_reasoning_effort ?? prev.ai_reasoning_effort,
        ai_codex_model: result.ai_codex_model ?? prev.ai_codex_model,
        ai_codex_command: result.ai_codex_command ?? prev.ai_codex_command,
        ai_codex_reasoning_effort: result.ai_codex_reasoning_effort ?? prev.ai_codex_reasoning_effort,
        ai_configured: result.ai_configured ?? prev.ai_configured,
        ai_max_output_tokens: result.ai_max_output_tokens ?? prev.ai_max_output_tokens,
        ai_context_window: result.ai_context_window ?? prev.ai_context_window,
      } : prev)
      qc.invalidateQueries({ queryKey: QK.settings })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: ({ name, provider }: { name: string; provider: string }) =>
      api.deleteAiProfile(name, provider),
    onSuccess: (result) => {
      setProfiles(result.profiles)
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_profiles: result.profiles,
      } : prev)
    },
  })

  const payload = () => ({
    provider,
    base_url: baseUrl,
    api_key: apiKey || undefined,
    model: isCodexProvider ? codexModel : model,
    ...(isOpenAIProvider ? { reasoning_effort: reasoningEffort } : {}),
    codex_command: isCodexProvider ? CODEX_COMMAND : codexCommand,
    codex_reasoning_effort: isCodexProvider ? codexReasoningEffort : '',
    user_agent: customUa ? userAgent : '',
    max_output_tokens: toPositiveInt(maxOutputTokens),
    context_window: toPositiveInt(contextWindow),
  })

  const save = useMutation({
    mutationFn: () => api.saveAiSettings(payload()),
    onSuccess: (result) => {
      setSaved(true)
      setApiKey('')
      if (result.profiles) setProfiles(result.profiles)
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_provider: result.ai_provider ?? provider,
        ai_base_url: baseUrl,
        ai_model: result.ai_model ?? (isCodexProvider ? codexModel : model),
        ai_openai_model: result.ai_openai_model ?? model,
        ai_reasoning_effort: result.ai_reasoning_effort ?? reasoningEffort,
        ai_codex_model: result.ai_codex_model ?? codexModel,
        ai_codex_command: result.ai_codex_command ?? (isCodexProvider ? CODEX_COMMAND : codexCommand),
        ai_codex_reasoning_effort: result.ai_codex_reasoning_effort ?? (isCodexProvider ? codexReasoningEffort : ''),
        ai_configured: result.ai_configured ?? (isCodexProvider ? true : (apiKey ? true : prev.ai_configured)),
        ai_max_output_tokens: result.ai_max_output_tokens ?? toPositiveInt(maxOutputTokens),
        ai_context_window: result.ai_context_window ?? toPositiveInt(contextWindow),
        ai_profiles: result.profiles ?? prev.ai_profiles,
        ...(apiKey ? {
          has_ai_key: true,
          ai_api_key_masked: `${apiKey.slice(0, 4)}......${apiKey.slice(-4)}`,
        } : {}),
      } : prev)
      qc.invalidateQueries({ queryKey: QK.settings })
      setTimeout(() => setSaved(false), 2000)
    },
  })

  const saveToProfiles = useMutation({
    mutationFn: () => api.saveAiProfile(payload()),
    onSuccess: (result) => {
      setProfiles(result.profiles)
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_profiles: result.profiles,
      } : prev)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    },
  })

  const clear = useMutation({
    mutationFn: () => api.clearAiSettings(),
    onSuccess: () => {
      setConfirmClear(false)
      setActiveProfile(null)
      setProvider(OPENAI_COMPAT_PROVIDER)
      setSelectedPresetLabel(PRESETS[0].label)
      setBaseUrl('')
      setApiKey('')
      setModel('')
      setReasoningEffort(DEFAULT_REASONING_EFFORT)
      setCodexModel('')
      setCodexReasoningEffort('')
      setCodexCommand(CODEX_COMMAND)
      directDrafts.current = {
        custom: { baseUrl: '', model: '' },
        openai: { baseUrl: 'https://api.openai.com/v1', model: DEFAULT_OPENAI_MODEL },
      }
      setMaxOutputTokens('8192')
      setContextWindow('64000')
      setTestResult(null)
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_provider: OPENAI_COMPAT_PROVIDER,
        ai_base_url: '',
        ai_model: '',
        ai_openai_model: '',
        ai_reasoning_effort: DEFAULT_REASONING_EFFORT,
        ai_codex_model: '',
        ai_codex_command: CODEX_COMMAND,
        ai_codex_reasoning_effort: '',
        ai_max_output_tokens: 8192,
        ai_context_window: 64000,
        has_ai_key: false,
        ai_configured: false,
        ai_api_key_masked: '',
      } : prev)
      qc.invalidateQueries({ queryKey: QK.settings })
    },
  })

  const genRandomUa = () => {
    const major = 128 + Math.floor(Math.random() * 8)
    const platforms = [
      'Windows NT 10.0; Win64; x64',
      'Macintosh; Intel Mac OS X 10_15_7',
      'X11; Linux x86_64',
    ]
    const pf = platforms[Math.floor(Math.random() * platforms.length)]
    setUserAgent(`Mozilla/5.0 (${pf}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36`)
  }

  const handlePreset = (p: AiPreset) => {
    setSelectedPresetLabel(p.label)
    setApiKey('')
    if (p.custom) {
      setProvider(OPENAI_COMPAT_PROVIDER)
      setBaseUrl(directDrafts.current.custom.baseUrl)
      setModel(directDrafts.current.custom.model)
      return
    }
    if (p.provider === CODEX_PROVIDER) {
      setProvider(CODEX_PROVIDER)
      setCodexModel(p.model)
      setCodexReasoningEffort(DEFAULT_CODEX_REASONING_EFFORT)
      setCodexCommand(CODEX_COMMAND)
      return
    }
    const nextProvider = p.provider ?? OPENAI_COMPAT_PROVIDER
    setProvider(nextProvider)
    if (nextProvider === OPENAI_PROVIDER) {
      setBaseUrl(directDrafts.current.openai.baseUrl)
      setModel(directDrafts.current.openai.model)
    } else {
      setBaseUrl(p.url)
      setModel(p.model)
    }
  }

  const handleBaseUrlChange = (value: string) => {
    setBaseUrl(value)
    if (selectedPreset.custom) directDrafts.current.custom.baseUrl = value
    if (isOpenAIProvider) directDrafts.current.openai.baseUrl = value
  }

  const handleModelChange = (value: string) => {
    setModel(value)
    if (selectedPreset.custom) directDrafts.current.custom.model = value
    if (isOpenAIProvider) directDrafts.current.openai.model = value
  }

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      if (canSave) await api.saveAiSettings(payload())
      const r = await api.strategyAiTest()
      setTestResult({ ok: r.ok, msg: r.ok ? `连通成功 · ${r.model ?? provider}` : (r.error ?? '未知错误') })
    } catch (e: any) {
      setTestResult({ ok: false, msg: String(e?.message ?? '测试失败') })
    } finally {
      setTesting(false)
    }
  }

  const handleSaveAndActivate = () => {
    save.mutate()
  }

  const handleSaveOnly = () => {
    saveToProfiles.mutate()
  }

  return (
    <HighlightContext.Provider value={highlight ?? ''}>
      <div className="flex gap-5 h-[calc(100vh-120px)]">
        <div className="w-72 shrink-0 flex flex-col">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium text-foreground">已配置模型</h3>
            <span className="text-[10px] text-muted">{profiles.length} 个</span>
          </div>

          <div className="flex-1 min-h-0 overflow-y-auto space-y-1.5 pr-1">
            {profiles.length === 0 ? (
              <div className="text-center py-8">
                <Sparkles className="h-8 w-8 text-muted/20 mx-auto mb-2" />
                <div className="text-xs text-muted/50">暂无配置</div>
                <div className="text-[10px] text-muted/30 mt-1">在右侧添加新模型</div>
              </div>
            ) : (
              profiles.map((profile, idx) => {
                const isActive = activeProfile?.name === profile.name && activeProfile?.provider === profile.provider
                return (
                  <ModelListItem
                    key={`${profile.provider}-${profile.name}-${idx}`}
                    profile={profile}
                    isActive={isActive}
                    onSelect={() => {
                      handleSelectProfile(profile)
                      activateMutation.mutate({ name: profile.name, provider: profile.provider })
                    }}
                    onDelete={() => {
                      setConfirmDelete({ name: profile.name, provider: profile.provider })
                    }}
                  />
                )
              })
            )}
          </div>
        </div>

        <div className="flex-1 min-w-0 overflow-y-auto pr-1">
          <div className="space-y-5 max-w-2xl">
            <Card
              icon={Plug}
              title="连接状态"
              anchor="ai-connection"
              right={
                configured && (
                  <button
                    onClick={handleTest}
                    disabled={testing}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn bg-elevated hover:bg-elevated/80 text-xs text-secondary transition-colors duration-150 ease-smooth disabled:opacity-50"
                  >
                    {testing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wifi className="h-3 w-3" />}
                    {testing ? '测试中' : '测试'}
                  </button>
                )
              }
            >
              <div className="flex items-center gap-3">
                <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${configured ? 'bg-emerald-400/10 text-emerald-400' : 'bg-amber-400/10 text-amber-400'}`}>
                  {configured ? <Wifi className="h-4.5 w-4.5" /> : <WifiOff className="h-4.5 w-4.5" />}
                </div>
                <div className="min-w-0">
                  <div className="text-sm font-medium text-foreground">{configured ? 'AI 已连接' : 'AI 未配置'}</div>
                  <div className="text-xs text-muted mt-0.5 truncate">
                    {configured
                      ? (savedCodexProvider
                        ? `${s?.ai_codex_command ?? CODEX_COMMAND} · ${codexModelLabel(s?.ai_model, s?.ai_codex_reasoning_effort)}`
                        : `${s?.ai_model} · ${s?.ai_api_key_masked}`)
                      : (isCodexProvider ? '使用本机 codex exec, 此处无需填写 API Key。' : '配置 API Key 后即可使用 AI 功能。')}
                  </div>
                </div>
              </div>
              {testResult && (
                <div className={`mt-3 rounded-btn border px-3 py-2 text-xs flex items-center gap-2 ${testResult.ok ? 'border-emerald-400/20 bg-emerald-400/[0.04] text-emerald-400' : 'border-danger/20 bg-danger/[0.04] text-danger'}`}>
                  <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${testResult.ok ? 'bg-emerald-400' : 'bg-danger'}`} />
                  {testResult.msg}
                </div>
              )}
            </Card>

            <Card
              icon={Zap}
              title="快速预设"
              subtitle={
                <span className="text-[10px] text-amber-400/70 flex items-center gap-1 ml-2">
                  <Shield className="h-3 w-3" />
                  {isCodexProvider
                    ? 'Codex CLI 模式会复用本机已登录的 Codex 账户, 个股、财务、复盘等分析上下文会发送给 OpenAI/Codex。保存即表示确认仅在本机或可信内网使用。'
                    : 'API Key 仅保存在本机项目文件中, 不会上传到任何服务器。请妥善保管。'}
                </span>
              }
            >
              <div className="space-y-3">
                <PresetDropdown
                  value={selectedPresetLabel}
                  onChange={label => {
                    const p = PRESETS.find(item => item.label === label) ?? PRESETS[0]
                    handlePreset(p)
                  }}
                  options={PRESETS.map(p => ({ label: p.label, value: p.label }))}
                />
                {selectedPreset && (
                  <div className="rounded-btn border border-border/30 bg-base/30 px-3 py-2 text-[11px] leading-relaxed">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="text-secondary">{selectedPreset.description}</span>
                    </div>
                    {selectedPreset.website && (
                      <a
                        href={selectedPreset.website}
                        target="_blank"
                        rel="noreferrer"
                        className="mt-1 inline-flex items-center gap-1 text-muted hover:text-accent transition-colors"
                      >
                        {selectedPreset.websiteLabel}
                        <ExternalLink className="h-3 w-3" />
                      </a>
                    )}
                  </div>
                )}
              </div>
            </Card>

            <Card
              icon={Settings2}
              title={configTitle}
              right={
                <span className="inline-flex items-center gap-1.5 text-[10px] text-muted/60" title={isCodexProvider ? 'Use local Codex CLI via codex exec' : 'Use OpenAI-compatible Chat Completions API'}>
                  <span className="rounded-full border border-border/40 bg-base/50 px-1.5 py-px font-mono">{isCodexProvider ? 'codex exec' : 'Chat Completions'}</span>
                  {isCodexProvider ? 'CLI' : '接口'}
                </span>
              }
            >
              <div className="space-y-4">
                {isCodexProvider ? (
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <Field label="CLI 命令" hint="固定使用默认 codex 命令, 由后端自动解析本机 Codex Desktop/CLI, 不支持自定义可执行路径。">
                      <div className={`${INPUT_CLS} flex items-center text-muted/80 select-none`} aria-label="Codex CLI command">
                        {CODEX_COMMAND}
                      </div>
                    </Field>
                    <Field label="模型 / 推理档" hint={selectedCodexModelOption.hint}>
                      <select
                        value={codexModelSelectValue}
                        onChange={e => {
                          const value = e.target.value
                          const option = codexModelOptions.find(item => item.value === value) ?? CODEX_MODEL_OPTIONS[0]
                          setCodexModel(option.model)
                          setCodexReasoningEffort(option.effort)
                        }}
                        className={INPUT_CLS}
                      >
                        {codexModelOptions.map(option => (
                          <option key={option.value || 'codex-local-default'} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </Field>
                  </div>
                ) : (
                  <>
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                      <Field label="API 地址">
                        <input type="text" value={baseUrl} onChange={e => handleBaseUrlChange(e.target.value)} placeholder="https://api.zhaji.dev/v1" className={INPUT_CLS} />
                      </Field>
                      <Field label="模型">
                        <input type="text" value={model} onChange={e => handleModelChange(e.target.value)} placeholder="gpt-5.6-sol" className={INPUT_CLS} />
                      </Field>
                    </div>

                    {isOpenAIProvider && (
                      <div className="rounded-lg border border-accent/15 bg-accent/[0.03] p-3">
                        <div className="mb-2.5">
                          <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent">OpenAI 专属</span>
                        </div>
                        <div className="max-w-xs">
                          <Field label="推理强度">
                            <input type="text" value={reasoningEffort} onChange={e => setReasoningEffort(e.target.value)} placeholder={DEFAULT_REASONING_EFFORT} className={INPUT_CLS} />
                          </Field>
                        </div>
                      </div>
                    )}

                    <Field label="API Key">
                      <div className="flex gap-2">
                        <div className="flex-1 relative">
                          <input type={showKey ? 'text' : 'password'} value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={configured ? `${s?.ai_api_key_masked} · 留空不修改` : 'sk-...'} className={`${INPUT_CLS} pr-9`} />
                          <button
                            onClick={() => setShowKey(v => !v)}
                            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted/40 hover:text-muted"
                            tabIndex={-1}
                            aria-label={showKey ? '隐藏' : '显示'}
                          >
                            {showKey ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                          </button>
                        </div>
                        <button
                          onClick={handleTest}
                          disabled={testing || !apiKey}
                          className="h-9 px-3 rounded-lg border border-border/50 text-xs text-secondary hover:text-accent hover:border-accent/30 disabled:opacity-40 transition-all flex items-center gap-1.5 shrink-0"
                        >
                          {testing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wifi className="h-3 w-3" />}
                          测试
                        </button>
                      </div>
                    </Field>

                    <div className="border-t border-border/20" />

                    <div className="space-y-2">
                      <div className="flex items-center justify-between">
                        <Field label="自定义 User-Agent" inline>
                          <Toggle checked={customUa} onChange={() => setCustomUa(v => !v)} />
                        </Field>
                      </div>
                      {customUa && (
                        <div className="flex gap-2">
                          <input type="text" value={userAgent} onChange={e => setUserAgent(e.target.value)} placeholder="粘贴浏览器 User-Agent" className={`${INPUT_CLS} flex-1`} />
                          <button
                            type="button"
                            onClick={genRandomUa}
                            title="随机生成浏览器 User-Agent"
                            className="h-9 px-2.5 rounded-lg border border-border/50 text-xs text-secondary hover:text-accent hover:border-accent/30 transition-all flex items-center gap-1.5 shrink-0"
                          >
                            <Shuffle className="h-3 w-3" /> 随机
                          </button>
                        </div>
                      )}
                    </div>
                  </>
                )}

                <div className="border-t border-border/20 pt-4">
                  <div className="grid grid-cols-2 gap-4">
                    <Field label="输出上限 max_tokens" hint="所有 AI 任务的输出 token 上限, 任务请求会被钳制到此值; 默认 8192">
                      <input type="number" min={1} value={maxOutputTokens} onChange={e => setMaxOutputTokens(e.target.value)} placeholder="8192" className={INPUT_CLS} />
                    </Field>
                    <Field label="上下文窗口 (输入上限)" hint="输入估算超出此窗口时会报错并提示调大; 默认 64000">
                      <input type="number" min={1} value={contextWindow} onChange={e => setContextWindow(e.target.value)} placeholder="64000" className={INPUT_CLS} />
                    </Field>
                  </div>
                </div>
              </div>
            </Card>

            <div className="flex gap-2">
              <button onClick={handleSaveAndActivate} disabled={save.isPending || !canSave} className="flex-1 h-10 rounded-xl bg-accent text-white text-sm font-semibold flex items-center justify-center gap-2 hover:bg-accent/90 disabled:opacity-40 transition-all">
                {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />}
                {save.isPending ? '保存中...' : saved ? '已保存' : '保存并激活'}
              </button>
              <button onClick={handleSaveOnly} disabled={saveToProfiles.isPending || !canSave} className="h-10 px-4 rounded-xl bg-elevated text-secondary text-sm flex items-center justify-center gap-1.5 hover:bg-elevated/80 disabled:opacity-40 transition-all shrink-0" title="仅保存到列表，不切换当前模型">
                <Plus className="h-4 w-4" />
                仅保存
              </button>
              {configured && (
                <button onClick={() => setConfirmClear(true)} disabled={clear.isPending} className="h-10 px-4 rounded-xl bg-elevated text-secondary hover:text-danger text-sm flex items-center justify-center gap-1.5 hover:bg-elevated/80 disabled:opacity-50 transition-all shrink-0" title="Clear AI provider configuration">
                  <Trash2 className="h-4 w-4" />
                  清空
                </button>
              )}
            </div>
          </div>
        </div>

        {confirmClear && createPortal(
          <div className="fixed inset-0 z-50 flex items-center justify-center">
            <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setConfirmClear(false)} />
            <div className="relative w-[90vw] max-w-[380px] rounded-card border border-border bg-base shadow-2xl p-6">
              <h3 className="text-sm font-medium text-foreground mb-2">清空 AI 配置</h3>
              <p className="text-xs text-secondary mb-5 leading-relaxed">
                这会清空已保存的 provider、API Key、API 地址、模型和 Codex CLI 命令。之后可以重新配置。
              </p>
              <div className="flex items-center justify-end gap-2">
                <button onClick={() => setConfirmClear(false)} className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors">
                  取消
                </button>
                <button onClick={() => clear.mutate()} disabled={clear.isPending} className="px-3 py-1.5 rounded-btn bg-danger/15 text-danger hover:bg-danger/25 text-sm font-medium transition-colors disabled:opacity-50">
                  {clear.isPending ? '清空中...' : '确认'}
                </button>
              </div>
            </div>
          </div>,
          document.body,
        )}

        {confirmDelete && createPortal(
          <div className="fixed inset-0 z-50 flex items-center justify-center">
            <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setConfirmDelete(null)} />
            <div className="relative w-[90vw] max-w-[380px] rounded-card border border-border bg-base shadow-2xl p-6">
              <h3 className="text-sm font-medium text-foreground mb-2">删除模型配置</h3>
              <p className="text-xs text-secondary mb-5">
                确认删除「{confirmDelete.name}」？此操作不可撤销。
              </p>
              <div className="flex items-center justify-end gap-2">
                <button onClick={() => setConfirmDelete(null)} className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors">
                  取消
                </button>
                <button
                  onClick={() => {
                    deleteMutation.mutate({ name: confirmDelete.name, provider: confirmDelete.provider })
                    setConfirmDelete(null)
                  }}
                  disabled={deleteMutation.isPending}
                  className="px-3 py-1.5 rounded-btn bg-danger/15 text-danger hover:bg-danger/25 text-sm font-medium transition-colors disabled:opacity-50"
                >
                  {deleteMutation.isPending ? '删除中...' : '确认删除'}
                </button>
              </div>
            </div>
          </div>,
          document.body,
        )}
      </div>
    </HighlightContext.Provider>
  )
}

const HighlightContext = createContext('')

interface CardProps {
  icon: React.ComponentType<{ className?: string }>
  title: string
  right?: React.ReactNode
  subtitle?: React.ReactNode
  children: React.ReactNode
  anchor?: string
}

function Card({ icon: Icon, title, right, subtitle, children, anchor }: CardProps) {
  const highlight = useContext(HighlightContext)
  const { ref, flash } = useCardFlash(anchor ? highlight : undefined, anchor ?? '')
  const inner = (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <Icon className="h-4 w-4 text-secondary" />
          <h2 className="text-sm font-medium text-foreground">{title}</h2>
          {subtitle}
        </div>
        {right}
      </div>
      {children}
    </section>
  )
  if (!anchor) return inner
  return (
    <div ref={ref} id={anchor} className={cardFlashCls(flash)}>
      {inner}
    </div>
  )
}

function Field({ label, hint, inline, children }: {
  label: string
  hint?: string
  inline?: boolean
  children: React.ReactNode
}) {
  if (inline) {
    return (
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[10px] text-muted/50 uppercase tracking-wider">{label}</div>
          {hint && <div className="text-[10px] text-muted mt-0.5">{hint}</div>}
        </div>
        {children}
      </div>
    )
  }
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] text-muted/50 uppercase tracking-wider">{label}</div>
      {children}
      {hint && <div className="text-[10px] text-muted">{hint}</div>}
    </div>
  )
}

function Toggle({ checked, onChange }: { checked: boolean; onChange: () => void }) {
  return (
    <button
      type="button"
      onClick={onChange}
      className={`relative inline-flex h-5 w-9 items-center rounded-full shrink-0 transition-colors duration-200 ${checked ? 'bg-accent' : 'bg-elevated'}`}
      aria-pressed={checked}
    >
      <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${checked ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
    </button>
  )
}
