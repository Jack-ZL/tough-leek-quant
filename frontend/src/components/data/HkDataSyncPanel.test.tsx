// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type HkDataStatus } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { HkDataSyncPanel } from './HkDataSyncPanel'

vi.mock('@/lib/api', () => ({ api: {
  hkSyncInstruments: vi.fn(), hkSyncDaily: vi.fn(), hkSyncEnriched: vi.fn(),
} }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const onStart = vi.fn()

/** 全 null = 从来没同步过 (本机真实状态: 港股目录全是 0 B) */
function emptyStatus(): HkDataStatus {
  return {
    instruments: null, daily: null, enriched: null,
    index_daily: null, index_enriched: null, adj_factor: null,
    financials: {
      synced: false, rows: 0, symbols: 0, latest_report_date: null,
      lot_size_coverage: null, tables: { income: 0, balance: 0 },
    },
    storage: { total_size_mb: 0 },
    checked_at: '2026-11-19T15:06:42Z',
  }
}

function fullStatus(over: Partial<HkDataStatus> = {}): HkDataStatus {
  return {
    ...emptyStatus(),
    instruments: {
      rows: 5, symbols_covered: 5, named: 5, latest_as_of: '2026-11-19',
      hkd_counters: 3, cny_counters: 2, industries: 2,
    },
    daily: {
      rows: 0, earliest_date: '2026-11-18', latest_date: '2026-11-19',
      trading_days: 2, symbols_covered: 3,
    },
    enriched: {
      rows: 0, earliest_date: '2026-11-18', latest_date: '2026-11-19',
      trading_days: 2, symbols_covered: 3, fields: 41,
    },
    adj_factor: {
      rows: 3, symbols_covered: 3, has_coverage: true, coverage_unreadable: false,
      covered: 3, uncovered: 0, coverage_updated_at: '2026-11-19T10:00:00+08:00',
    },
    ...over,
  }
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  client.setQueryData(QK.hkStatus, emptyStatus())
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render(status: HkDataStatus | undefined, isRunning = false) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <HkDataSyncPanel status={status} isLoading={false} isRunning={isRunning} onStart={onStart} />
  </QueryClientProvider>))
  await act(async () => { await new Promise(r => setTimeout(r, 10)) })
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))!
  expect(button, `找不到按钮: ${text}`).toBeTruthy()
  await act(async () => { button.click(); await new Promise(r => setTimeout(r, 10)) })
}
function disabled(text: string) {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.includes(text))!
  expect(button, `找不到按钮: ${text}`).toBeTruthy()
  return (button as HTMLButtonElement).disabled
}

it('画像读取中显示加载态, 读取失败不炸', async () => {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <HkDataSyncPanel status={undefined} isLoading isRunning={false} onStart={onStart} />
  </QueryClientProvider>))
  expect(host.textContent).toContain('读取港股数据画像')
  // 同一个 root 直接换 props 重渲染 (unmount 后再 render 会抛)
  await act(async () => root.render(<QueryClientProvider client={client}>
    <HkDataSyncPanel status={undefined} isLoading={false} isRunning={false} onStart={onStart} />
  </QueryClientProvider>))
  expect(host.textContent).toContain('画像读取失败')
})

it('从未同步: 七块全显示未同步, 日K与重算入口禁用', async () => {
  await render(emptyStatus())
  expect(host.textContent).toContain('港股维表为空')
  expect(host.textContent).toContain('复权因子从未同步')
  expect(host.textContent).toContain('整轮零行')
  // 🔴 维表为空 ⇒ 日K必被后端 409 拒; 没有日K ⇒ 重算无从下手
  expect(disabled('同步日K')).toBe(true)
  expect(disabled('重算指标')).toBe(true)
  expect(disabled('同步维表')).toBe(false)
})

it('双柜台分开报, 不混成一个标的数', async () => {
  await render(fullStatus())
  expect(host.textContent).toContain('3 只港币柜台')
  expect(host.textContent).toContain('另有 2 只人民币柜台')
})

it('个股日K的 rows=0 是「没统计」哨兵, 大数字取交易日数', async () => {
  await render(fullStatus())
  expect(host.textContent).toContain('2 个交易日')
  expect(host.textContent).not.toContain('0 行')
})

it('维表同步返回 empty 不当成功 (抓取失败与上游真空同报此值)', async () => {
  vi.mocked(api.hkSyncInstruments).mockResolvedValue({ status: 'empty', count: 0 })
  await render(emptyStatus())
  await click('同步维表')
  expect(toast).toHaveBeenCalledWith(expect.stringContaining('没拿到港股维表数据'), 'error')
  expect(toast).not.toHaveBeenCalledWith(expect.stringContaining('已同步'), 'success')
})

it('维表同步成功报条数并失效画像', async () => {
  vi.mocked(api.hkSyncInstruments).mockResolvedValue({ status: 'ok', count: 2687 })
  const spy = vi.spyOn(client, 'invalidateQueries')
  await render(emptyStatus())
  await click('同步维表')
  expect(toast).toHaveBeenCalledWith('港股维表已同步: 2687 条', 'success')
  // 🔴 后端港股画像没有服务端缓存, 这条失效是唯一的一致性保证
  expect(spy).toHaveBeenCalledWith({ queryKey: QK.hkStatus })
})

it('复权凭据损坏与「没跑过」分开报, 且不给绕过闸门的按钮', async () => {
  await render(fullStatus({
    adj_factor: {
      rows: 0, symbols_covered: 0, has_coverage: false, coverage_unreadable: true,
      covered: 0, uncovered: 0, coverage_updated_at: null,
    },
  }))
  expect(host.textContent).toContain('读不出来')
  expect(host.textContent).toContain('版本迁移')
  expect(host.textContent).not.toContain('复权因子从未同步')
  // 只有三个同步入口, 没有「强制重算复权」这类拆闸门的按钮
  expect(host.querySelectorAll('button')).toHaveLength(3)
})

it('部分标的未覆盖时说明这就是行数偏少的原因', async () => {
  await render(fullStatus({
    adj_factor: {
      rows: 3, symbols_covered: 5, has_coverage: true, coverage_unreadable: false,
      covered: 3, uncovered: 2, coverage_updated_at: '2026-11-19T10:00:00+08:00',
    },
  }))
  expect(host.textContent).toContain('2 只标的不在复权凭据覆盖范围内')
  expect(host.textContent).toContain('逐个拒算')
  expect(host.textContent).toContain('不是计算出错')
})

it('有日K无 enriched 时提示可本地重算, 点击交出 job_id', async () => {
  vi.mocked(api.hkSyncEnriched).mockResolvedValue({ job_id: 'hk-enr-job' })
  await render(fullStatus({ enriched: null }))
  expect(host.textContent).toContain('已有日K但没有 Enriched')
  expect(host.textContent).toContain('不发网络请求')
  await click('重算指标')
  expect(api.hkSyncEnriched).toHaveBeenCalledWith()
  expect(onStart).toHaveBeenCalledWith('hk-enr-job')
})

it('日K同步受理后交出 job_id 给页面轮询', async () => {
  vi.mocked(api.hkSyncDaily).mockResolvedValue({ job_id: 'hk-daily-job' })
  await render(fullStatus())
  await click('同步日K')
  expect(onStart).toHaveBeenCalledWith('hk-daily-job')
})

it('有任务在跑时三个入口全禁用 (与 A 股共用重任务槽)', async () => {
  await render(fullStatus(), true)
  expect(disabled('同步维表')).toBe(true)
  expect(disabled('同步日K')).toBe(true)
  expect(disabled('重算指标')).toBe(true)
  expect(host.textContent).toContain('已有数据任务在跑')
})
