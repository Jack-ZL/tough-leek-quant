// @vitest-environment jsdom
/**
 * 通达信板块文件（.blk/.ebk）导入的前端路由与呈现。
 *
 * 只覆盖前端自己负责的部分：扩展名分流、建议分组名预填、未匹配原因展示。
 * 解析规则本身由后端 tests/test_watchlist_blk.py 覆盖，这里全部走 mock。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { WatchlistImportDialog } from './WatchlistImportDialog'
import type { WatchlistGroup, WatchlistImportResult } from '@/lib/api'
import { DEFAULT_WATCHLIST_GROUP_COLOR } from '@/lib/watchlist-group-colors'

const mocks = vi.hoisted(() => ({
  blk: vi.fn(),
  csv: vi.fn(),
  image: vi.fn(),
  codes: vi.fn(),
  groupCreate: vi.fn(),
  batchAdd: vi.fn(),
  toast: vi.fn(),
  close: vi.fn(),
}))
vi.mock('@/lib/api', () => ({
  api: {
    watchlistImportBlk: mocks.blk,
    watchlistImportCsv: mocks.csv,
    watchlistImportImage: mocks.image,
    watchlistImportCodes: mocks.codes,
    watchlistGroupCreate: mocks.groupCreate,
  },
}))
vi.mock('@/components/Toast', () => ({ toast: mocks.toast }))
vi.mock('@/lib/useSharedMutations', () => ({
  useWatchlistBatchAdd: () => ({ mutateAsync: mocks.batchAdd, isPending: false }),
}))

const groups: WatchlistGroup[] = [
  { id: 'g1', name: '持仓', color: DEFAULT_WATCHLIST_GROUP_COLOR },
]

const result = (over: Partial<WatchlistImportResult> = {}): WatchlistImportResult => ({
  provider: 'blk',
  codes: ['600519'],
  candidates: [
    { code: '600519', symbol: '600519.SH', name: '贵州茅台', matched: true, already_in_watchlist: false },
  ],
  matched_count: 1,
  unmatched_count: 0,
  ...over,
})

const container = document.createElement('div')
document.body.appendChild(container)
let root: Root | undefined
let qc: QueryClient

async function render(groupId: string | null = null) {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  root = createRoot(container)
  await act(async () => {
    root!.render(
      <QueryClientProvider client={qc}>
        <WatchlistImportDialog
          open
          onClose={mocks.close}
          groupId={groupId}
          groups={groups}
          existingBySymbol={new Map()}
        />
      </QueryClientProvider>,
    )
  })
  await act(async () => { await new Promise(r => setTimeout(r, 10)) })
}

/** 往唯一的 file input 塞文件并触发 change，模拟用户选择/拖拽。 */
async function pickFiles(files: File[]) {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  expect(input).toBeTruthy()
  Object.defineProperty(input, 'files', { configurable: true, value: files })
  await act(async () => { input.dispatchEvent(new Event('change', { bubbles: true })) })
  await act(async () => { await new Promise(r => setTimeout(r, 10)) })
}

const blkFile = (name = 'my.blk') => new File(['1600519\n'], name, { type: 'application/octet-stream' })
const text = () => document.body.textContent ?? ''
const newGroupInput = () =>
  document.querySelector<HTMLInputElement>('input[aria-label="新分组名称"], input[placeholder*="分组"]')

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  Object.values(mocks).forEach(m => m.mockReset())
  mocks.blk.mockResolvedValue(result())
  mocks.csv.mockResolvedValue(result({ provider: 'csv' }))
  mocks.codes.mockResolvedValue(result({ provider: 'codes' }))
})
afterEach(() => {
  act(() => root?.unmount())
  root = undefined
  qc?.clear()
  container.innerHTML = ''
})

it('.blk 走板块接口，不误入 CSV/截图通道', async () => {
  await render()
  await pickFiles([blkFile('my.blk')])
  expect(mocks.blk).toHaveBeenCalledTimes(1)
  expect(mocks.csv).not.toHaveBeenCalled()
  expect(mocks.image).not.toHaveBeenCalled()
  expect(mocks.blk.mock.calls[0][0].name).toBe('my.blk')
})

it('.ebk 同样走板块接口（大小写不敏感）', async () => {
  await render()
  await pickFiles([blkFile('自选.EBK')])
  expect(mocks.blk).toHaveBeenCalledTimes(1)
  expect(mocks.csv).not.toHaveBeenCalled()
})

it('.csv 仍走 CSV 接口（分流未回归）', async () => {
  await render()
  await pickFiles([new File(['600519\n'], 'a.csv', { type: 'text/csv' })])
  expect(mocks.csv).toHaveBeenCalledTimes(1)
  expect(mocks.blk).not.toHaveBeenCalled()
})

it('一次多个板块文件 → 报错且不发请求', async () => {
  await render()
  await pickFiles([blkFile('a.blk'), blkFile('b.blk')])
  expect(mocks.blk).not.toHaveBeenCalled()
  expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining('单独导入'), 'error')
})

it('板块文件与截图混选 → 报错且不发请求', async () => {
  await render()
  await pickFiles([blkFile('a.blk'), new File(['x'], 'p.png', { type: 'image/png' })])
  expect(mocks.blk).not.toHaveBeenCalled()
  expect(mocks.csv).not.toHaveBeenCalled()
  expect(mocks.image).not.toHaveBeenCalled()
})

it('用文件名主干预填「新建分组」并展开输入框', async () => {
  await render()
  mocks.blk.mockResolvedValue(result({ suggested_group_name: '茅台概念' }))
  await pickFiles([blkFile()])
  const input = newGroupInput()
  expect(input).toBeTruthy()
  expect(input!.value).toBe('茅台概念')
})

it('CSV 导入不预填分组名（建议名是板块通道独有的）', async () => {
  await render()
  await pickFiles([new File(['600519\n'], 'a.csv', { type: 'text/csv' })])
  expect(mocks.csv).toHaveBeenCalledTimes(1)
  expect(newGroupInput()).toBeNull()
})

it('后端没给建议名时不预填、不自动展开新建分组', async () => {
  await render()
  mocks.blk.mockResolvedValue(result({ suggested_group_name: '' }))
  await pickFiles([blkFile()])
  expect(newGroupInput()?.value ?? '').toBe('')
})

it('已选中目标分组时不抢着新建分组', async () => {
  await render('g1')
  mocks.blk.mockResolvedValue(result({ suggested_group_name: '茅台概念' }))
  await pickFiles([blkFile()])
  expect(mocks.blk).toHaveBeenCalledTimes(1)
  expect(newGroupInput()?.value ?? '').not.toBe('茅台概念')
})

it('未匹配候选展示后端给的具体原因，而不是笼统文案', async () => {
  await render()
  mocks.blk.mockResolvedValue(
    result({
      codes: [],
      matched_count: 0,
      unmatched_count: 2,
      candidates: [
        {
          code: '603703', symbol: null, name: null, matched: false, already_in_watchlist: false,
          reason: '市场标识 .SZ 与主数据 603703.SH 不符',
        },
        {
          code: '1A0001', symbol: null, name: null, matched: false, already_in_watchlist: false,
          reason: '不是 6 位数字代码（指数如 1A0001 不支持）',
        },
      ],
    }),
  )
  await pickFiles([blkFile()])
  // 未匹配项默认折叠在「已略过」里，先展开
  const toggle = [...document.querySelectorAll('button')].find(b => b.textContent?.includes('已略过'))
  expect(toggle).toBeTruthy()
  await act(async () => { toggle!.click() })
  const body = text()
  expect(body).toContain('市场标识 .SZ 与主数据 603703.SH 不符')
  expect(body).toContain('不是 6 位数字代码（指数如 1A0001 不支持）')
})

it('没有 reason 时回退到通用未匹配文案', async () => {
  await render()
  mocks.blk.mockResolvedValue(
    result({
      codes: [],
      matched_count: 0,
      unmatched_count: 1,
      candidates: [
        { code: '999999', symbol: null, name: null, matched: false, already_in_watchlist: false },
      ],
    }),
  )
  await pickFiles([blkFile()])
  const toggle = [...document.querySelectorAll('button')].find(b => b.textContent?.includes('已略过'))
  await act(async () => { toggle!.click() })
  expect(text()).toContain('未能匹配证券主数据，已跳过')
})

it('拖拽区文案提到通达信板块，accept 含 .blk/.ebk', async () => {
  await render()
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
  expect(input.accept).toContain('.blk')
  expect(input.accept).toContain('.ebk')
  expect(text()).toContain('通达信板块')
})
