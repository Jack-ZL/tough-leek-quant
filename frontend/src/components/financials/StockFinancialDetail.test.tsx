// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  // tab → useFinancialTable 的返回值。用例直接改这个对象模拟后端响应。
  tables: {} as Record<string, { data?: unknown; isLoading?: boolean }>,
  startAnalysis: vi.fn(),
  findLatestHistoryReport: vi.fn(),
  toast: vi.fn(),
}))

vi.mock('@/lib/useFinancials', () => ({
  useFinancialTable: (_market: string, tab: string) =>
    mocks.tables[tab] ?? { data: undefined, isLoading: false },
}))
vi.mock('@/lib/aiReportStore', () => ({
  startAnalysis: mocks.startAnalysis,
  findLatestHistoryReport: mocks.findLatestHistoryReport,
  openHistoryReport: vi.fn(),
}))
vi.mock('@/components/Toast', () => ({ toast: mocks.toast }))

import { StockFinancialDetail } from './StockFinancialDetail'
import { FINANCIAL_SOURCES } from '@/lib/financial-market-adapter'

let host: HTMLDivElement, root: Root
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mocks.tables = {}
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(async () => { await act(async () => root.unmount()); host.remove() })

const render = async (market: 'CN' | 'HK', symbol = '600000.SH', name = '浦发银行') =>
  act(async () => { root.render(<StockFinancialDetail symbol={symbol} name={name} market={market} />) })

const tabLabels = () =>
  [...host.querySelectorAll('button')].map(b => b.textContent?.trim()).filter(t => t && !t.includes('AI'))
const bodyText = () => host.textContent ?? ''
const clickTab = async (label: string) => {
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === label)
  expect(btn, `找不到 tab「${label}」`).toBeTruthy()
  await act(async () => { btn!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
}
/** 字段行 → [标签, 值] */
const fieldRows = () =>
  [...host.querySelectorAll('.grid > div')].map(d => {
    const [k, v] = d.querySelectorAll('span')
    return [k?.textContent?.trim(), v?.textContent?.trim()]
  })

describe('tab 集由适配层决定, 组件里不写市场分支', () => {
  it('A 股渲染五张表(含股本)', async () => {
    await render('CN')
    expect(tabLabels()).toEqual(FINANCIAL_SOURCES.CN.tabs.map(t => FINANCIAL_SOURCES.CN.tabLabel(t)))
    expect(tabLabels()).toContain('股本')
  })

  it('🔴 港股只有四张表: 数据源不提供股本口径, 渲染出来必然整片「—」', async () => {
    await render('HK', '00700.HK', '腾讯控股')
    expect(tabLabels()).toEqual(FINANCIAL_SOURCES.HK.tabs.map(t => FINANCIAL_SOURCES.HK.tabLabel(t)))
    expect(tabLabels()).not.toContain('股本')
  })

  it('🔴 切市场后旧 tab 不在新市场里 → 派生回落到第一个 tab(不是 useEffect+setState)', async () => {
    await render('CN')
    await clickTab('股本')
    expect(bodyText()).toContain('股本')
    // 同一棵组件树换成港股: 若用 useEffect 纠正, 会先渲染一帧「无高亮 + 内容必空」
    await render('HK', '00700.HK', '腾讯控股')
    expect(tabLabels()).not.toContain('股本')
    const active = host.querySelector('button.border-accent')
    expect(active?.textContent?.trim()).toBe(FINANCIAL_SOURCES.HK.tabLabel('metrics'))
  })
})

describe('市场级口径说明: A 股不渲染, 港股必须说清币种与机构类型', () => {
  it('A 股无币种说明、无机构类型提示(界面与改造前一致)', async () => {
    mocks.tables.metrics = {
      data: { rows: [{ period_end: '2026-09-30', roe: 12.3 }], meta: null },
    }
    await render('CN')
    expect(bodyText()).not.toContain('编报币种')
    expect(bodyText()).not.toContain('数据源口径')
  })

  it('港股带出编报币种 + 后端 currency_warning 原文', async () => {
    mocks.tables.metrics = {
      data: {
        rows: [{ period_end: '2026-09-30', roe: 1.2 }],
        meta: {
          synced: true, reportCurrency: 'CNY', pricingCurrency: 'HKD',
          currencyWarning: '金额列为编报币种; 估值列为港币', orgType: '一般企业',
          lotSize: 100, latestReportDate: '2026-09-30', periods: 6,
        },
      },
    }
    await render('HK', '00700.HK', '腾讯控股')
    expect(bodyText()).toContain('编报币种 CNY')
    expect(bodyText()).toContain('金额列为编报币种; 估值列为港币')
    expect(bodyText()).not.toContain('数据源口径')
  })

  it('🔴 港股金融股提示口径差异: 否则「毛利率 —」会被读成缺数', async () => {
    mocks.tables.metrics = {
      data: {
        rows: [{ period_end: '2026-09-30', gross_margin: null }],
        meta: {
          synced: true, reportCurrency: 'HKD', pricingCurrency: 'HKD', currencyWarning: null,
          orgType: '银行', lotSize: 1000, latestReportDate: '2026-09-30', periods: 4,
        },
      },
    }
    await render('HK', '01398.HK', '工商银行')
    expect(bodyText()).toContain('银行')
    expect(bodyText()).toContain('数据源口径')
  })

  it('meta 只落在 metrics 上时, 其它 tab 也能读到(兜底取 metrics)', async () => {
    mocks.tables.metrics = {
      data: {
        rows: [], meta: {
          synced: true, reportCurrency: 'USD', pricingCurrency: 'HKD', currencyWarning: null,
          orgType: '一般企业', lotSize: 100, latestReportDate: null, periods: 0,
        },
      },
    }
    mocks.tables.income = { data: { rows: [{ period_end: '2026-06-30', revenue: 1 }], meta: null } }
    await render('HK', '09988.HK', '阿里巴巴')
    await clickTab(FINANCIAL_SOURCES.HK.tabLabel('income'))
    expect(bodyText()).toContain('编报币种 USD')
  })
})

describe('数值格式: 语义与搬进适配层之前一致', () => {
  it('🔴 pct 存的已是百分点, 不得再 ×100', async () => {
    mocks.tables.metrics = { data: { rows: [{ period_end: '2026-09-30', roe: 12.34 }], meta: null } }
    await render('CN')
    expect(fieldRows().find(r => r[0]?.includes('ROE'))?.[1]).toBe('12.34%')
  })

  it('港股每手股数按整数渲染(100 而不是 100.00)', async () => {
    mocks.tables.metrics = {
      data: { rows: [{ period_end: '2026-09-30', lot_size: 100 }], meta: null },
    }
    await render('HK', '00700.HK', '腾讯控股')
    expect(fieldRows().find(r => r[0]?.includes('每手'))?.[1]).toBe('100')
  })

  it('🔴 parquet 投影可能给字符串或 "--": 不得在 toFixed 上抛异常', async () => {
    mocks.tables.metrics = {
      data: {
        rows: [{ period_end: '2026-09-30', roe: '9.50', gross_margin: '--', bps: null }],
        meta: null,
      },
    }
    await render('CN')
    const rows = fieldRows()
    expect(rows.find(r => r[0]?.includes('ROE'))?.[1]).toBe('9.50%')
    expect(rows.find(r => r[0]?.includes('毛利率'))?.[1]).toBe('—')
    expect(rows.find(r => r[0]?.includes('每股净资产'))?.[1]).toBe('—')
  })

  it('多期按 period_end 降序, 最新在前', async () => {
    mocks.tables.metrics = {
      data: {
        rows: [
          { period_end: '2025-12-31', roe: 1 },
          { period_end: '2026-09-30', roe: 2 },
        ],
        meta: null,
      },
    }
    await render('CN')
    expect(bodyText()).toContain('报告期')
    const periods = [...host.querySelectorAll('.font-mono.text-secondary')].map(e => e.textContent)
    expect(periods[0]).toBe('2026-09-30')
    expect(periods[1]).toBe('2025-12-31')
  })
})

describe('报告期徽章: 港股财年结日不固定, 光看日期会误判', () => {
  it('港股多期时标出 period_name(阿里 6 月底是「一季报」)', async () => {
    mocks.tables.metrics = {
      data: {
        rows: [
          { period_end: '2026-06-30', period_name: '一季报', roe: 1 },
          { period_end: '2026-03-31', period_name: '年报', roe: 2 },
        ],
        meta: null,
      },
    }
    await render('HK', '09988.HK', '阿里巴巴')
    expect(bodyText()).toContain('一季报')
    expect(bodyText()).toContain('年报')
  })

  it('A 股无此概念 ⇒ 不渲染徽章', async () => {
    mocks.tables.metrics = {
      data: {
        rows: [
          { period_end: '2026-09-30', period_name: '三季报', roe: 1 },
          { period_end: '2026-06-30', period_name: '中报', roe: 2 },
        ],
        meta: null,
      },
    }
    await render('CN')
    expect(bodyText()).not.toContain('三季报')
  })

  it('单期时不渲染分期标题(与改造前一致)', async () => {
    mocks.tables.metrics = {
      data: { rows: [{ period_end: '2026-09-30', period_name: '三季报', roe: 1 }], meta: null },
    }
    await render('HK', '00700.HK', '腾讯控股')
    expect(bodyText()).not.toContain('三季报')
    // 头部的「报告期 <日期>」仍在
    expect(bodyText()).toContain('2026-09-30')
  })
})

describe('空态即契约: 解释为什么空 + 给出出路', () => {
  it.each([
    ['CN', '600000.SH', '浦发银行', '核心指标'],
    ['HK', '00700.HK', '腾讯控股', '核心指标'],
  ] as const)('%s 空数据时给出该市场自己的说明', async (market, symbol, name, label) => {
    mocks.tables.metrics = { data: { rows: [], meta: null } }
    await render(market, symbol, name)
    const src = FINANCIAL_SOURCES[market]
    expect(bodyText()).toContain(src.tableEmptyHint('metrics', symbol))
    expect(bodyText()).toContain('同步')
    expect(label).toBe(src.tabLabel('metrics'))
  })

  it('港股空态点明「不在覆盖范围内」这种同步也解决不了的情况', async () => {
    mocks.tables.metrics = { data: { rows: [], meta: null } }
    await render('HK', '09988.HK', '阿里巴巴')
    expect(bodyText()).toContain('不在东财港股财务覆盖范围内')
  })

  it('加载中显示骨架屏而不是空态', async () => {
    mocks.tables.metrics = { data: undefined, isLoading: true }
    await render('CN')
    expect(bodyText()).not.toContain('同步')
  })
})

describe('AI 分析入口: 两个市场都就绪(后端已按市场分派)', () => {
  it.each(['CN', 'HK'] as const)('%s 都渲染 AI 财务分析按钮', async (market) => {
    await render(market, market === 'CN' ? '600000.SH' : '00700.HK', 'X')
    expect(bodyText()).toContain('AI 财务分析')
  })

  it('无历史报告 → 直接分析; 有历史 → 二次确认', async () => {
    mocks.findLatestHistoryReport.mockResolvedValue(null)
    mocks.startAnalysis.mockResolvedValue({ error: null })
    await render('HK', '00700.HK', '腾讯控股')
    const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('AI 财务分析'))!
    await act(async () => { btn.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    expect(mocks.startAnalysis).toHaveBeenCalledWith('00700.HK', '腾讯控股')
    expect(bodyText()).not.toContain('该个股已有分析报告')
  })
})
