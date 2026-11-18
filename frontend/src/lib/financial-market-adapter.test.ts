// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  financialStatus: vi.fn(),
  financialSync: vi.fn(),
  financialMetrics: vi.fn(),
  financialIncome: vi.fn(),
  financialBalanceSheet: vi.fn(),
  financialCashFlow: vi.fn(),
  financialShares: vi.fn(),
  hkFinancialsStatus: vi.fn(),
  hkFinancials: vi.fn(),
  hkSyncFinancials: vi.fn(),
}))

vi.mock('@/lib/api', () => ({ api: mocks }))

import {
  ALL_FINANCIAL_TABS,
  FINANCIAL_SOURCES,
  financialSource,
  requireFinancialSource,
  type FinancialTab,
} from './financial-market-adapter'
import { QK } from './queryKeys'
import { MARKETS } from './market'
import { marketContent } from './marketContent'

const cn = FINANCIAL_SOURCES.CN
const hk = FINANCIAL_SOURCES.HK

/** `QueryObserverOptions.queryFn` 的类型含 initial-promise 哨兵符号, 直接调用过不了
 *  `tsc -b`; 测试里只关心它打的是哪个 api 方法, 故收窄成无参函数。 */
const runQuery = (q: { queryFn?: unknown }) => (q.queryFn as () => Promise<any>)()

/** 港股规范表列集 —— 权威: `backend/app/plugins/eastmoney/hk_financials.py` 的
 *  `HK_CANONICAL_PROJECTION`(11/5/3/3) + `HK_ONLY_CANONICAL_FIELDS`(9) + 键列。
 *  🔴 这份清单是「前端字段表不得引用后端不存在的列」的钉子: 后端改投影时这里会红。 */
const HK_CANONICAL_COLUMNS = new Set([
  // 键 / 时间
  'symbol', 'period_end', 'announce_date', 'fiscal_year', 'report_date_ms', 'period_end_ms',
  // metrics 投影 (11)
  'eps_basic', 'bps', 'revenue_yoy', 'net_income_yoy', 'roa', 'roe',
  'gross_margin', 'net_margin', 'current_ratio', 'debt_to_asset_ratio',
  'operating_cash_to_revenue',
  // income 投影 (5)
  'revenue', 'operating_profit', 'total_profit', 'net_income_attributable', 'basic_eps',
  // balance_sheet 投影 (3)
  'total_assets', 'total_liabilities', 'total_equity',
  // cash_flow 投影 (3)
  'net_operating_cash_flow', 'net_investing_cash_flow', 'net_financing_cash_flow',
  // 港股独有 (9)
  'name', 'org_type', 'period_name', 'date_type_code', 'pricing_currency',
  'lot_size', 'pe_ttm', 'pb_ttm', 'total_market_cap',
])

beforeEach(() => {
  vi.clearAllMocks()
})

describe('financialSource: 市场 → 数据源查表, 无控制流分支', () => {
  it('MARKETS 里每个市场都在表里(Record 类型的运行时兜底)', () => {
    expect(Object.keys(FINANCIAL_SOURCES).sort()).toEqual([...MARKETS].sort())
    for (const m of MARKETS) expect(FINANCIAL_SOURCES[m].market).toBe(m)
  })

  it.each(MARKETS)('%s 的 content 与 marketContent 同源', (m) => {
    expect(FINANCIAL_SOURCES[m].content).toBe(marketContent(m))
  })

  it('🔴 认不出的市场返回 null, **不兜底 CN**(与 indexSource 刻意相反)', () => {
    // 本函数决定打哪个端点 + 用哪套字段表, 属请求层: 兜底等于拿港股代码去查 A 股
    // (静默返回空), 或把 A 股字段表套到港股行上渲染出一整片「—」。
    expect(financialSource(null)).toBeNull()
    expect(financialSource('CN')).toBe(cn)
    expect(financialSource('HK')).toBe(hk)
  })

  it('requireFinancialSource 对每个合法市场都非空(不用非空断言)', () => {
    for (const m of MARKETS) expect(requireFinancialSource(m)).toBe(FINANCIAL_SOURCES[m])
  })
})

describe('能力位: 差异是数据, 页面据此隐藏入口而不是渲染必然失败的按钮', () => {
  it('A 股: 每表可单独同步 / 无 job / 有股本表', () => {
    expect(cn.capabilities).toEqual({
      perTableSync: true, jobSync: false, sharesTable: true, aiAnalysis: true,
    })
  })

  it('港股: 整批 job 同步 / 无每表入口 / 无股本表', () => {
    expect(hk.capabilities).toEqual({
      perTableSync: false, jobSync: true, sharesTable: false, aiAnalysis: true,
    })
  })

  it('🔴 AI 分析两个市场都就绪(后端 financial_analyzer 已按 infer_market 分派)', () => {
    for (const m of MARKETS) expect(FINANCIAL_SOURCES[m].capabilities.aiAnalysis).toBe(true)
  })

  it('sharesTable 与 tabs 里有没有 shares 一致(两处不得漂移)', () => {
    for (const m of MARKETS) {
      const src = FINANCIAL_SOURCES[m]
      expect(src.tabs.includes('shares')).toBe(src.capabilities.sharesTable)
    }
  })

  it('tabs 是 ALL_FINANCIAL_TABS 的子集且无重复', () => {
    for (const m of MARKETS) {
      const tabs = FINANCIAL_SOURCES[m].tabs
      expect(new Set(tabs).size).toBe(tabs.length)
      for (const t of tabs) expect(ALL_FINANCIAL_TABS).toContain(t)
    }
  })
})

describe('字段表: 只列该市场后端真的会返回的列', () => {
  it('港股字段 key 全部落在后端规范列集内(不得引用不存在的列)', () => {
    for (const tab of hk.tabs) {
      for (const def of hk.fieldDefs(tab)) {
        expect(HK_CANONICAL_COLUMNS.has(def.key), `HK ${tab}.${def.key}`).toBe(true)
      }
    }
  })

  it('🔴 港股刻意不照抄 A 股字段表: 周转率/现金比率族后端不提供, 列出来只会整片渲染「—」', () => {
    const hkKeys = new Set(hk.tabs.flatMap(t => hk.fieldDefs(t).map(d => d.key)))
    // A 股 metrics 独有、港股宽表没有的口径(实测 A 股 29 列 vs 港股 26 列)
    for (const absent of ['inventory_turnover', 'eps_diluted', 'roe_diluted', 'ocfps']) {
      expect(hkKeys.has(absent), absent).toBe(false)
    }
  })

  it('港股有 A 股没有的估值/交易口径(PE/PB/总市值/每手股数)', () => {
    const keys = hk.fieldDefs('metrics').map(d => d.key)
    for (const k of ['pe_ttm', 'pb_ttm', 'total_market_cap', 'lot_size']) {
      expect(keys).toContain(k)
    }
    // 每手股数是整数, 渲染成 100.00 是错的
    expect(hk.fieldDefs('metrics').find(d => d.key === 'lot_size')?.fmt).toBe('int')
  })

  it('港股利润表用港式/IFRS 科目名, 不复用 A 股标签', () => {
    const labels = hk.fieldDefs('income').map(d => d.label)
    expect(labels).toContain('营业额')
    expect(labels).toContain('股东应占溢利')
    expect(labels).toContain('除税前溢利')
    expect(labels).not.toContain('营业收入')
    expect(labels).not.toContain('归母净利润')
  })

  it('A 股字段表与搬家前逐字一致(回归钉: 搬进适配层不得顺手改口径)', () => {
    expect(cn.fieldDefs('metrics').map(d => d.key)).toEqual([
      'eps_basic', 'eps_diluted', 'bps', 'ocfps', 'roe', 'roe_diluted', 'roa',
      'gross_margin', 'net_margin', 'debt_to_asset_ratio', 'revenue_yoy',
      'net_income_yoy', 'operating_cash_to_revenue', 'inventory_turnover',
    ])
    expect(cn.fieldDefs('income')).toHaveLength(16)
    expect(cn.fieldDefs('balance_sheet')).toHaveLength(19)
    expect(cn.fieldDefs('cash_flow').map(d => d.key)).toEqual([
      'net_operating_cash_flow', 'net_investing_cash_flow', 'net_financing_cash_flow',
      'capex', 'net_cash_change',
    ])
    expect(cn.fieldDefs('shares').map(d => d.key)).toEqual(['total_shares', 'float_shares'])
  })

  it('A 股 pct 语义不变: 存的是百分点, 标签不得暗示要 ×100', () => {
    const roe = cn.fieldDefs('metrics').find(d => d.key === 'roe')
    expect(roe?.fmt).toBe('pct')
    expect(roe?.label).toBe('净资产收益率 ROE')
  })

  it('每个 tab 的字段 key 无重复(重复会渲染两行同名字段)', () => {
    for (const m of MARKETS) {
      const src = FINANCIAL_SOURCES[m]
      for (const tab of src.tabs) {
        const keys = src.fieldDefs(tab).map(d => d.key)
        expect(new Set(keys).size, `${m}/${tab}`).toBe(keys.length)
      }
    }
  })

  it('tabLabel 对全部 tab 都有非空标签(含该市场不渲染的)', () => {
    for (const m of MARKETS) {
      const src = FINANCIAL_SOURCES[m]
      for (const tab of ALL_FINANCIAL_TABS) {
        expect(src.tabLabel(tab).length, `${m}/${tab}`).toBeGreaterThan(0)
      }
    }
  })
})

describe('query key: 两个市场前缀互斥, 跨市场不得沿用缓存', () => {
  it('状态 key 前缀不同', () => {
    expect(cn.statusQuery().queryKey).toEqual(QK.financialsStatus)
    expect(hk.statusQuery().queryKey).toEqual(QK.hkFinancialsStatus)
    expect(cn.statusQuery().queryKey[0]).not.toBe(hk.statusQuery().queryKey[0])
  })

  it('表 key 前缀不同', () => {
    expect(cn.tableQuery('metrics', '600000.SH').queryKey[0]).toBe('financials')
    expect(hk.tableQuery('metrics', '00700.HK').queryKey[0]).toBe('hk-financials')
  })

  it('🔴 港股四张表**共用一个 key**: 后端一次返回全部四表, 靠 select 各取一片 ⇒ 去重成一次请求', () => {
    const keys = hk.tabs.map(t => hk.tableQuery(t, '00700.HK').queryKey)
    for (const k of keys) expect(k).toEqual(keys[0])
    expect(keys[0]).toEqual(QK.hkFinancials('00700.HK', 8))
  })

  it('港股 select 各取自己那张表, 且带上标的级 meta', () => {
    const payload = {
      symbol: '00700.HK', synced: true, org_type: '一般企业', pricing_currency: 'HKD',
      lot_size: 100, latest_report_date: '2026-09-30', periods: 6, report_currency: 'CNY',
      currency_warning: '金额列为编报币种', tables: {
        metrics: [{ period_end: '2026-09-30', roe: 1.2 }],
        income: [{ period_end: '2026-09-30', revenue: 3.4 }],
        balance_sheet: [], cash_flow: [],
      },
    }
    const sel = hk.tableQuery('income', '00700.HK').select as (d: any) => any
    const out = sel(payload)
    expect(out.rows).toEqual([{ period_end: '2026-09-30', revenue: 3.4 }])
    expect(out.meta).toMatchObject({ synced: true, reportCurrency: 'CNY', orgType: '一般企业', lotSize: 100 })
    // 缺表不得抛异常
    expect((hk.tableQuery('cash_flow', '00700.HK').select as any)({ tables: {} }).rows).toEqual([])
    expect((hk.tableQuery('cash_flow', '00700.HK').select as any)(undefined)).toEqual({ rows: [], meta: null })
  })

  it('A 股 select 把 {data:[…]} 归一化成 {rows, meta:null}(A 股端点无标的级元信息)', () => {
    const sel = cn.tableQuery('metrics', '600000.SH').select as (d: any) => any
    expect(sel({ data: [{ period_end: '2026-09-30' }] })).toEqual({
      rows: [{ period_end: '2026-09-30' }], meta: null,
    })
    expect(sel(undefined)).toEqual({ rows: [], meta: null })
  })

  it('invalidateKeys 与本市场状态 key 同前缀(失效打得中)', () => {
    for (const m of MARKETS) {
      const src = FINANCIAL_SOURCES[m]
      const prefix = src.statusQuery().queryKey[0]
      expect(src.invalidateKeys).toEqual([[prefix]])
    }
    expect(cn.invalidateKeys[0][0]).not.toBe(hk.invalidateKeys[0][0])
  })

  it('该市场没有的表 → 停用查询(never fetched), 保证 hooks 数量恒定', () => {
    const q = hk.tableQuery('shares' as FinancialTab, '00700.HK')
    expect(q.enabled).toBe(false)
  })

  it('未选标的时不发请求', () => {
    for (const m of MARKETS) {
      const src = FINANCIAL_SOURCES[m]
      for (const tab of src.tabs) expect(src.tableQuery(tab, undefined).enabled).toBe(false)
    }
  })
})

describe('请求分派: 各打各的端点', () => {
  it('A 股状态 → /api/financials/status, 同步中每 3s 轮询、空闲不轮询', async () => {
    mocks.financialStatus.mockResolvedValue({ available: true, syncing: false, tables: {}, last_sync: {} })
    const q = cn.statusQuery()
    await runQuery(q)
    expect(mocks.financialStatus).toHaveBeenCalledTimes(1)
    const interval = q.refetchInterval as (q: any) => number | false
    expect(interval({ state: { data: { syncing: true } } })).toBe(3_000)
    expect(interval({ state: { data: { syncing: false } } })).toBe(false)
  })

  it('港股状态 → /api/hk/financials/status, 不轮询(进度在 job store 里)', async () => {
    mocks.hkFinancialsStatus.mockResolvedValue({ synced: false, rows: 0, symbols: 0, tables: {} })
    const q = hk.statusQuery()
    await runQuery(q)
    expect(mocks.hkFinancialsStatus).toHaveBeenCalledTimes(1)
    expect(q.refetchInterval).toBeUndefined()
  })

  it('A 股每张表打各自的端点', async () => {
    mocks.financialMetrics.mockResolvedValue({ data: [] })
    mocks.financialIncome.mockResolvedValue({ data: [] })
    mocks.financialBalanceSheet.mockResolvedValue({ data: [] })
    mocks.financialCashFlow.mockResolvedValue({ data: [] })
    mocks.financialShares.mockResolvedValue({ data: [] })
    for (const tab of cn.tabs) await runQuery(cn.tableQuery(tab, '600000.SH'))
    expect(mocks.financialMetrics).toHaveBeenCalledWith('600000.SH')
    expect(mocks.financialIncome).toHaveBeenCalledWith('600000.SH')
    expect(mocks.financialBalanceSheet).toHaveBeenCalledWith('600000.SH')
    expect(mocks.financialCashFlow).toHaveBeenCalledWith('600000.SH')
    expect(mocks.financialShares).toHaveBeenCalledWith('600000.SH')
  })

  it('🔴 港股四张表都打同一个端点(一次拿全), 不是一表一请求', async () => {
    mocks.hkFinancials.mockResolvedValue({ symbol: '00700.HK', synced: true, tables: {} })
    for (const tab of hk.tabs) await runQuery(hk.tableQuery(tab, '00700.HK'))
    expect(mocks.hkFinancials).toHaveBeenCalledTimes(4)
    for (const call of mocks.hkFinancials.mock.calls) {
      expect(call).toEqual(['00700.HK', { limit: 8 }])
    }
  })

  it('A 股同步回 {started, reason}; 港股同步回 {jobId}(整批重任务)', async () => {
    mocks.financialSync.mockResolvedValue({ synced: { started: false, reason: 'already running' } })
    expect(await cn.syncTable('metrics')).toEqual({ started: false, reason: 'already running' })

    mocks.hkSyncFinancials.mockResolvedValue({ job_id: 'j-1', reused: false })
    expect(await hk.syncTable('all')).toEqual({ jobId: 'j-1', started: true })
    expect(mocks.hkSyncFinancials).toHaveBeenCalledWith({})
  })
})

describe('状态归一化: 两个市场 payload 形状不同, 页面只认一个视图', () => {
  it('A 股: 五张卡片按同步序排列, 带每表 rows/symbols/lastSync', () => {
    const v = (cn.statusQuery().select as any)({
      available: true,
      syncing: true,
      tables: { metrics: { rows: 10, symbols: 5 }, income: { rows: 7, symbols: 5 } },
      last_sync: { metrics: '2026-11-17T01:00:00Z' },
    })
    expect(v.available).toBe(true)
    expect(v.syncing).toBe(true)
    expect(v.syncOrder).toEqual(['metrics', 'income', 'balance_sheet', 'cash_flow', 'shares'])
    expect(v.cards.map((c: any) => c.key)).toEqual(v.syncOrder)
    expect(v.cards[0]).toEqual({
      key: 'metrics', label: '核心指标', rows: 10, symbols: 5, lastSync: '2026-11-17T01:00:00Z',
    })
    // 缺表 → 0 而不是 null(A 股确实提供这两个口径, 0 就是「同步过但没数据」)
    expect(v.cards[1]).toMatchObject({ rows: 7, symbols: 5, lastSync: null })
    expect(v.cards[4]).toMatchObject({ key: 'shares', rows: 0, symbols: 0, lastSync: null })
  })

  it('A 股 status 未加载(undefined)不抛异常', () => {
    const v = (cn.statusQuery().select as any)(undefined)
    expect(v).toMatchObject({ available: false, syncing: false, syncOrder: cn.tabs.slice() })
    expect(v.cards.every((c: any) => c.rows === 0)).toBe(true)
  })

  it('港股未同步: available=false, 卡片全 0, 长表 symbols=null(该表不提供标的数口径)', () => {
    const v = (hk.statusQuery().select as any)({
      synced: false, rows: 0, symbols: 0, latest_report_date: null,
      lot_size_coverage: null, tables: { income: 0, balance: 0 },
    })
    expect(v.available).toBe(false)
    expect(v.notes).toEqual([])
    expect(v.cards.map((c: any) => c.key)).toEqual(['main_indicator', 'income', 'balance'])
    expect(v.cards[1].symbols).toBeNull()
    expect(v.cards[2].symbols).toBeNull()
  })

  it('🔴 港股 syncing 恒 false: 它是 job 制, 进度不在 status payload 里(由页面轮询 job)', () => {
    const v = (hk.statusQuery().select as any)({ synced: true, rows: 1, symbols: 1, tables: {} })
    expect(v.syncing).toBe(false)
    expect(hk.capabilities.jobSync).toBe(true)
  })

  it('🔴 港股 syncOrder 为空: 没有每表 last_sync, 参与「第几张表完成」推算会说假话', () => {
    const v = (hk.statusQuery().select as any)({ synced: true, rows: 1, symbols: 1, tables: {} })
    expect(v.syncOrder).toEqual([])
    expect(v.cards.every((c: any) => c.lastSync === null)).toBe(true)
  })

  it('港股已同步: notes 给出最新报告期 / 每手股数覆盖 / 报告期类型', () => {
    const v = (hk.statusQuery().select as any)({
      synced: true, rows: 835, symbols: 835, latest_report_date: '2026-09-30',
      lot_size_coverage: 800, period_names: ['三季报', '中报', '年报'],
      tables: { income: 162, balance: 90 },
    })
    expect(v.available).toBe(true)
    expect(v.cards[0]).toMatchObject({ rows: 835, symbols: 835 })
    expect(v.cards[1].rows).toBe(162)
    expect(v.notes).toEqual([
      '最新报告期 2026-09-30',
      '每手股数已覆盖 800 只',
      '报告期类型 三季报 / 中报 / 年报',
    ])
  })

  it('港股 lot_size_coverage=0 也要出 note(0 是有效值, 不是缺失)', () => {
    const v = (hk.statusQuery().select as any)({
      synced: true, rows: 1, symbols: 1, lot_size_coverage: 0, tables: {},
    })
    expect(v.notes).toContain('每手股数已覆盖 0 只')
  })
})

describe('币种与机构类型: 不在前端另备一份文案, 也不得默认人民币', () => {
  it('A 股金额一律人民币元, 无歧义 ⇒ 不加说明行(保持既有界面不变)', () => {
    expect(cn.amountNote(null)).toBeNull()
    expect(cn.amountNote({ synced: true } as any)).toBeNull()
  })

  it('港股带出后端 currency_warning 原文 + 具体编报币种', () => {
    const note = hk.amountNote({
      synced: true, reportCurrency: 'CNY', currencyWarning: '金额列为编报币种; 估值列为港币',
    } as any)
    expect(note).toContain('编报币种 CNY')
    expect(note).toContain('金额列为编报币种; 估值列为港币')
  })

  it('🔴 编报币种未知时明说未知, 不默认人民币(长表未同步就取不到 CURRENCY_CODE)', () => {
    const note = hk.amountNote({ synced: true, reportCurrency: null, currencyWarning: null } as any)
    expect(note).toContain('编报币种未知')
    expect(note).not.toContain('CNY')
    expect(note).not.toContain('人民币')
  })

  it('无 meta 时不渲染币种行', () => {
    expect(hk.amountNote(null)).toBeNull()
  })

  it.each(['银行', '证券', '保险'])('港股金融股(%s)提示口径差异, 避免把「数据源不提供」读成「缺数」', (t) => {
    const hint = hk.orgTypeHint({ synced: true, orgType: t } as any)
    expect(hint).toContain(t)
    expect(hint).toContain('数据源口径')
  })

  it('港股一般企业 / 未知机构类型 / A 股 → 无提示', () => {
    expect(hk.orgTypeHint({ synced: true, orgType: '一般企业' } as any)).toBeNull()
    expect(hk.orgTypeHint({ synced: true, orgType: null } as any)).toBeNull()
    expect(hk.orgTypeHint(null)).toBeNull()
    expect(cn.orgTypeHint({ synced: true, orgType: '银行' } as any)).toBeNull()
  })
})

describe('报告期标签: 港股财年结日不固定, 光看日期会误判', () => {
  it('港股返回 period_name(阿里 6 月底是「一季报」, 因为财年 3 月底结束)', () => {
    expect(hk.periodBadge({ period_end: '2026-06-30', period_name: '一季报' })).toBe('一季报')
  })

  it('港股缺 period_name / 空串 → null(不渲染空徽章)', () => {
    expect(hk.periodBadge({ period_end: '2026-06-30' })).toBeNull()
    expect(hk.periodBadge({ period_name: '' })).toBeNull()
    expect(hk.periodBadge({})).toBeNull()
  })

  it('A 股无此概念 → 恒 null', () => {
    expect(cn.periodBadge({ period_end: '2026-09-30', period_name: '三季报' })).toBeNull()
  })
})

describe('空态即契约: 必须解释为什么空 + 给出出路', () => {
  it.each(MARKETS)('%s 每张表的空态都指向同步入口', (m) => {
    const src = FINANCIAL_SOURCES[m]
    for (const tab of src.tabs) {
      const hint = src.tableEmptyHint(tab, 'X')
      expect(hint).toContain('同步')
      expect(hint.length).toBeGreaterThan(10)
    }
  })

  it('港股空态带上标的代码, 并说明「刚同步过仍为空」的另一种原因', () => {
    const hint = hk.tableEmptyHint('metrics', '00700.HK')
    expect(hint).toContain('00700.HK')
    expect(hint).toContain('主指标宽表')
    expect(hint).toContain('不在东财港股财务覆盖范围内')
  })

  it('A 股空态用该表自己的中文名', () => {
    expect(cn.tableEmptyHint('balance_sheet')).toContain('资产负债表')
    expect(cn.tableEmptyHint('shares')).toContain('股本')
  })
})

describe('ownsSymbol: 「上次查看」跨市场不得恢复', () => {
  it('各市场只认自己后缀的 symbol', () => {
    expect(cn.ownsSymbol('600000.SH')).toBe(true)
    expect(cn.ownsSymbol('00700.HK')).toBe(false)
    expect(hk.ownsSymbol('00700.HK')).toBe(true)
    expect(hk.ownsSymbol('600000.SH')).toBe(false)
  })

  it('认不出的 symbol 两个市场都不认(不兜底)', () => {
    for (const m of MARKETS) expect(FINANCIAL_SOURCES[m].ownsSymbol('AAPL')).toBe(false)
  })
})

describe('页面文案: 说清这个市场有什么、没有什么', () => {
  it('港股副标题不含「股本」, A 股含', () => {
    expect(hk.pageSubtitle).not.toContain('股本')
    expect(cn.pageSubtitle).toContain('股本')
  })

  it('港股页头说明分开存储 + 无股本表的原因(不是「没同步」)', () => {
    expect(hk.headerHint).toContain('financials_hk')
    expect(hk.headerHint).toContain('无股本表')
    expect(hk.headerHint).toContain('数据源不提供')
  })

  it('A 股无额外页头说明(空串 ⇒ 页面不渲染那一行)', () => {
    expect(cn.headerHint).toBe('')
  })

  it('搜索占位/示例用本市场的代码格式', () => {
    expect(hk.searchPlaceholder).toContain('00700')
    expect(hk.searchExample).toContain('00700')
    expect(cn.searchPlaceholder).toContain('600000')
    expect(cn.searchExample).toContain('600000')
  })

  it('同步按钮文案区分市场(港股是整批, 不叫「全部同步」)', () => {
    expect(cn.syncAllLabel).toBe('全部同步')
    expect(hk.syncAllLabel).toContain('港股')
  })

  it('卡片栅格类名与卡片数匹配(港股 3 张卡不该占 5 列)', () => {
    expect(cn.cardGridClass).toContain('xl:grid-cols-5')
    expect(hk.cardGridClass).toContain('md:grid-cols-3')
    expect(hk.cardGridClass).not.toContain('grid-cols-5')
  })
})
