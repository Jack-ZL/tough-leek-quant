// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { FactorEditor } from './FactorEditor'

const mocks = vi.hoisted(() => ({
  validate: vi.fn(),
  trial: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
  library: vi.fn(),
  columns: vi.fn(),
  tdxMeta: vi.fn(),
}))
vi.mock('@/lib/api', () => ({
  api: {
    factorValidate: mocks.validate,
    factorTrial: mocks.trial,
    factorCustomCreate: mocks.create,
    factorCustomUpdate: mocks.update,
    factorLibrary: mocks.library,
    factorColumns: mocks.columns,
    factorTdxMeta: mocks.tdxMeta,
  },
}))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const DSL_SAMPLE = 'rank(-ts_sum(change_pct, 5))'
const TDX_SAMPLE = 'DIF:=EMA(C,12)-EMA(C,26);\nDEA:=EMA(DIF,9);\nMACD柱:(DIF-DEA)*2;'

const validation = (dslText = '') => ({
  ok: true, errors: [], dependencies: ['close'], referenced_factors: [],
  warmup_bars: 6, cross_sectional: false, dsl_text: dslText,
})

// 三种 kind 各一行: 前端要按「直接支持 / 宏展开 / 不支持」分区, 少一种就测不到分区逻辑
const META = {
  templates: [
    { id: 'macd_hist', name: 'MACD 柱', category: '趋势', kind: 'value', formula: 'DIF:=EMA(C,12);', description: '动能' },
    { id: 'ma_bull', name: '均线多头排列', category: '均线', kind: 'signal', formula: 'MA(C,5)>MA(C,10)', description: '排列' },
  ],
  compatibility: [
    { tdx: 'MA', maps_to: 'ts_mean', kind: 'operator', status: 'supported' },
    { tdx: 'CROSS', maps_to: 'A > B and REF(A,1) <= REF(B,1)', kind: 'macro', status: 'supported' },
    { tdx: 'ZIG', category: 'future', reason: '信号会重绘', kind: 'rejected', status: 'rejected' },
  ],
}

const LIBRARY = {
  factors: [{
    id: 'uf_tdx_1', label: '通达信动量', group: '自定义', kind: 'custom', version: 2,
    formula: 'MA(C,5)', direction: 'none', unit: '', warmup_bars: 6, pit: true,
    asset_types: ['stock'], stability: 'experimental', status: 'draft', scale_free: false,
    dependencies: ['close'], lang: 'tdx',
  }],
}

const container = document.createElement('div')
document.body.appendChild(container)
let root: Root
let qc: QueryClient

const byText = (text: string) =>
  [...container.querySelectorAll('button')].find(b => b.textContent?.trim() === text)
const byTextPrefix = (text: string) =>
  [...container.querySelectorAll('button')].find(b => b.textContent?.trim().startsWith(text))
const textarea = () => container.querySelector('textarea') as HTMLTextAreaElement
const text = () => container.textContent ?? ''
/** 兼容表分区 <details>: 按 summary 文字定位 (外层「函数兼容表」summary 不以分区名开头, 不会撞) */
const section = (label: string) =>
  [...container.querySelectorAll('details')].find(d => d.querySelector('summary')?.textContent?.trim().startsWith(label))

/** React 受控 input 要用原生 setter 改值, 否则 value tracker 认为没变、onChange 不触发 */
const typeInto = async (el: HTMLInputElement, value: string) => {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
  await act(async () => {
    setter.call(el, value)
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.validate.mockReset().mockResolvedValue(validation())
  mocks.trial.mockReset().mockResolvedValue({
    ok: true, n_dates: 10, null_ratio: 0, ic_mean: 0.05, ic_std: 0.1, ir: 0.5,
    ic_win_rate: 0.6, t_newey_west: 2.5, ic_series: [],
  })
  mocks.create.mockReset().mockResolvedValue({ ok: true, id: 'uf_new', version: 1 })
  mocks.update.mockReset().mockResolvedValue({ ok: true, id: 'uf_tdx_1', version: 3, status: 'draft' })
  mocks.library.mockReset().mockResolvedValue(LIBRARY)
  mocks.columns.mockReset().mockResolvedValue({ columns: [{ id: 'close', label: '收盘价', group: '价格', desc: '' }] })
  mocks.tdxMeta.mockReset().mockResolvedValue(META)
  vi.stubGlobal('confirm', vi.fn(() => true))
  root = createRoot(container)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(() => {
  act(() => root.unmount())
  qc.clear()
  container.innerHTML = ''
  vi.unstubAllGlobals()
})

/** 冲一轮宏任务: react-query 的 resolve 要跨多个微任务, 只 await Promise.resolve() 不够 */
const flush = async () => {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

const render = async (props: { editId?: string } = {}) => {
  await act(async () => {
    root.render(
      <QueryClientProvider client={qc}>
        <MemoryRouter><FactorEditor {...props} /></MemoryRouter>
      </QueryClientProvider>,
    )
  })
  // 因子库/字段是异步查询, 冲完才轮到 useEffect(?edit=) 恢复档位
  await flush()
}

it('算子选择器覆盖后端全部 29 个算子 (含第 2 批新加的 4 个)', async () => {
  await render()
  expect(text()).toContain('算子（29 个')
  // 后端能编译却在编辑器里点不出来 = 只能手打, 拼错才发现
  for (const sig of ['ts_ema(x,n)', 'ts_sma(x,n,m)', 'ts_barslast(c)', 'ts_barssince(c)']) {
    expect(text()).toContain(sig)
  }
})

it('切语言档位作废上一档的校验结果, 并把 lang 传给后端', async () => {
  await render()
  await act(async () => { byText('校验')!.click() })
  expect(text()).toContain('公式有效')
  expect(mocks.validate).toHaveBeenLastCalledWith(DSL_SAMPLE, 'dsl')

  await act(async () => { byText('通达信')!.click() })
  // 旧档位的「公式有效」不能留在屏上当成当前结果
  expect(text()).not.toContain('公式有效')
  // 没动过的样例公式跟着换档, 否则切过去就是一条必然报错的 DSL
  expect(textarea().value).toBe(TDX_SAMPLE)

  await act(async () => { byText('校验')!.click() })
  expect(mocks.validate).toHaveBeenLastCalledWith(TDX_SAMPLE, 'tdx')
})

it('用户写过的公式在切档时保留, 不被样例覆盖', async () => {
  await render()
  const el = textarea()
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!
  await act(async () => {
    setter.call(el, 'MA(C,5)>REF(MA(C,5),1)')
    el.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => { byText('通达信')!.click() })
  expect(textarea().value).toBe('MA(C,5)>REF(MA(C,5),1)')
})

it('通达信档渲染后端下发的模板与兼容表, 点模板整体替换公式', async () => {
  await render()
  // DSL 档不该出现通达信面板
  expect(text()).not.toContain('通达信公式模板')
  await act(async () => { byText('通达信')!.click() })
  await flush()   // 面板挂载后才发起 /tdx/meta 查询

  expect(text()).toContain('MACD 柱')
  expect(text()).toContain('均线多头排列')
  expect(text()).toContain('直接支持')
  expect(text()).toContain('ts_mean')

  // 模板按钮带「数值/信号」角标, 所以用前缀匹配
  await act(async () => { byTextPrefix('均线多头排列')!.click() })
  expect(textarea().value).toBe('MA(C,5)>MA(C,10)')
})

it('兼容表可按函数名筛选, 命中时自动展开被拒分区并给出原因', async () => {
  await render()
  await act(async () => { byText('通达信')!.click() })
  await flush()
  const filter = container.querySelector('input[aria-label="筛选通达信函数"]') as HTMLInputElement
  expect(filter).toBeTruthy()
  // 默认只展开「直接支持」: 那是最常查的一档, 31 条不支持摊开会淹没它
  expect(section('直接支持')?.open).toBe(true)
  expect(section('不支持')?.open).toBe(false)

  await typeInto(filter, 'ZIG')
  expect(text()).toContain('命中 1 / 3 个函数')
  // 命中项藏在折叠分区里等于没筛到 —— 有关键词时必须强制展开
  expect(section('不支持')?.open).toBe(true)
  // 被拒原因与归类必须可见: 只说"不支持"用户不知道下一步该怎么办
  expect(text()).toContain('信号会重绘')
  expect(text()).toContain('未来函数')
})

it('翻译预览展示后端返回的等价 DSL, 未校验时给提示而不是空白', async () => {
  await render()
  await act(async () => { byText('通达信')!.click() })
  expect(text()).toContain('点「校验」后显示')

  mocks.validate.mockResolvedValue(validation('ts_mean(close, 5)'))
  await act(async () => { byText('校验')!.click() })
  expect(text()).toContain('ts_mean(close, 5)')
})

it('编辑已有通达信因子时恢复语言档位与原文', async () => {
  await render({ editId: 'uf_tdx_1' })
  // 档位错了会静默错译: 同一段文本在 dsl/tdx 下语义不同
  expect(byText('通达信')!.getAttribute('aria-pressed')).toBe('true')
  expect(byText('DSL 公式')!.getAttribute('aria-pressed')).toBe('false')
  expect(textarea().value).toBe('MA(C,5)')
  // 恢复档位不该顺手触发校验
  expect(mocks.validate).not.toHaveBeenCalled()
})

it('保存时把 lang 一起提交, 后端才能按同一档位重新试算', async () => {
  await render()
  await act(async () => { byText('通达信')!.click() })
  await act(async () => { byText('校验')!.click() })
  await act(async () => { byText('试算 40 日')!.click() })

  const label = container.querySelector('input[placeholder^="名称"]') as HTMLInputElement
  await typeInto(label, '通达信动量')
  await act(async () => { byText('保存 (草稿态)')!.click() })

  expect(mocks.create).toHaveBeenCalledTimes(1)
  expect(mocks.create.mock.calls[0][0]).toMatchObject({ lang: 'tdx', label: '通达信动量', formula: TDX_SAMPLE })
  expect(mocks.trial).toHaveBeenLastCalledWith(expect.objectContaining({ lang: 'tdx' }))
})
