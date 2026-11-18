import {useQuery, useMutation, useQueryClient} from '@tanstack/react-query'
import {api} from '@/lib/api'
import {type Market} from '@/lib/market'
import {
	requireFinancialSource,
	type FinancialStatusView,
	type FinancialSyncOutcome,
	type FinancialTab,
	type FinancialTableData,
} from '@/lib/financial-market-adapter'
import {QK} from '@/lib/queryKeys'

/**
 * A 股财务 query keys。
 *
 * 🔴 权威定义已搬到 `lib/queryKeys.ts`: 市场适配层(`financial-market-adapter.ts`)
 * 也要拼这些 key, 而本文件反过来要 import 适配层 —— key 留在这里会形成
 * `useFinancials ↔ financial-market-adapter` 循环依赖。此处保留同名导出,
 * 既有 import 点(`FINANCIAL_QK.metrics(…)`)不必改。
 */
export const FINANCIAL_QK = {
	status: QK.financialsStatus,
	metrics: QK.financialsMetrics,
	income: QK.financialsIncome,
	balanceSheet: QK.financialsBalanceSheet,
	cashFlow: QK.financialsCashFlow,
	shares: QK.financialsShares,
}

export function useFinancialStatus() {
	return useQuery({
		queryKey: FINANCIAL_QK.status,
		queryFn: () => api.financialStatus(),
		staleTime: 60_000,
		// 同步进行中时每 3s 轮询,及时反映表数变化与同步完成;空闲时不轮询。
		refetchInterval: (query) => (query.state.data?.syncing ? 3_000 : false),
	})
}

export function financialMetricsQueryOptions(symbol?: string) {
	return {
		queryKey: FINANCIAL_QK.metrics(symbol),
		queryFn: () => api.financialMetrics(symbol),
		staleTime: 300_000,
	}
}

export function useFinancialMetrics(symbol?: string) {
	return useQuery({...financialMetricsQueryOptions(symbol), enabled: !!symbol})
}

export function useFinancialIncome(symbol?: string) {
	return useQuery({
		queryKey: FINANCIAL_QK.income(symbol),
		queryFn: () => api.financialIncome(symbol),
		enabled: !!symbol,
		staleTime: 300_000,
	})
}

export function useFinancialBalanceSheet(symbol?: string) {
	return useQuery({
		queryKey: FINANCIAL_QK.balanceSheet(symbol),
		queryFn: () => api.financialBalanceSheet(symbol),
		enabled: !!symbol,
		staleTime: 300_000,
	})
}

export function useFinancialCashFlow(symbol?: string) {
	return useQuery({
		queryKey: FINANCIAL_QK.cashFlow(symbol),
		queryFn: () => api.financialCashFlow(symbol),
		enabled: !!symbol,
		staleTime: 300_000,
	})
}

export function useFinancialShares(symbol?: string) {
	return useQuery({
		queryKey: FINANCIAL_QK.shares(symbol),
		queryFn: () => api.financialShares(symbol),
		enabled: !!symbol,
		staleTime: 300_000,
	})
}

export function useFinancialSync() {
	const qc = useQueryClient()
	return useMutation({
		mutationFn: (table: string) => api.financialSync(table),
		// 点击瞬间立即刷新 status: 让后端 is_syncing=True 马上反映到 UI,
		// 避免 mutation 阻塞(全量同步需数分钟)期间界面无变化。
		onMutate: () => {
			qc.invalidateQueries({queryKey: FINANCIAL_QK.status})
		},
		onSuccess: () => {
			qc.invalidateQueries({queryKey: FINANCIAL_QK.status})
			qc.invalidateQueries({queryKey: ['financials']})
		},
	})
}

// ===== 市场感知版(PR5 港股财务) =====
//
// 🔴 上面那批 A 股 hook 的**签名与返回形状一律不动**, 市场版只新增不替换:
//   - `useFinancialStatus` / `useFinancialSync` 还被 `components/data/FinancialSyncPanel.tsx`
//     (数据页) 消费;
//   - `useFinancialMetrics` / `financialMetricsQueryOptions` 还被 `components/StockPanel.tsx`
//     消费, 且 `StockPanel.quote-date.test.tsx` 用 `vi.mock('@/lib/useFinancials', …)`
//     **只 stub 了这两个名字** —— 改形状会让那个测试拿到 undefined。
// 市场版通过适配层的 `select` 把两个市场不同的原始 payload 归一化, 页面只认归一化后的类型。

/** 同步状态(归一化)。与 `useFinancialStatus` 共用同一个 query key, 靠 `select` 分视图。 */
export function useFinancialStatusView(market: Market) {
	return useQuery(requireFinancialSource(market).statusQuery())
}

/**
 * 某张表的数据(归一化)。
 *
 * 🔴 调用方必须对**全部** tab 无条件调用(hooks 数量恒定), 该市场没有的表由适配层
 * 返回 `enabled: false` 的停用查询 —— 不能按 `source.tabs` 条件调用 hook。
 */
export function useFinancialTable(market: Market, tab: FinancialTab, symbol?: string) {
	return useQuery(requireFinancialSource(market).tableQuery(tab, symbol))
}

/**
 * 触发同步。返回 `FinancialSyncOutcome`:
 * A 股 → `{started, reason}`(同步阻塞在请求里, 进度靠 status 轮询);
 * 港股 → `{jobId}`(重任务槽, 进度靠 `api.pipelineJob(jobId)` 轮询)。
 */
export function useFinancialTableSync(market: Market) {
	const qc = useQueryClient()
	const source = requireFinancialSource(market)
	const invalidate = () => {
		source.invalidateKeys.forEach(queryKey => qc.invalidateQueries({queryKey}))
	}
	return useMutation<FinancialSyncOutcome, Error, string>({
		mutationFn: (table: string) => source.syncTable(table),
		// 点击瞬间先失效一次: A 股让后端 is_syncing=True 立刻反映到 UI;
		// 港股此刻还没有 job_id, 真正的完成失效由页面的 job 轮询负责。
		onMutate: invalidate,
		onSuccess: invalidate,
	})
}

/** 归一化后的状态视图类型(页面/卡片渲染用)。 */
export type {FinancialStatusView, FinancialTableData}
