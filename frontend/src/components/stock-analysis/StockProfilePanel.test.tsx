// @vitest-environment jsdom
/**
 * 个股资料面板的市场无源契约 (PR5: stock_profile 港股口径适配)。
 *
 * 🔴 本文件存在的唯一理由是钉住一条**否定式伪造**的防线。后端对港股 symbol 返回
 * `state=market_unsupported`; 若前端把它当成普通的空结果渲染, 用户看到的是
 * 「该标的暂无**限售解禁**记录」—— 那句话的意思是「我们查过了, 确实没有」,
 * 而事实是**我们根本没查也查不了**(十类源全是 A 股专属披露口径)。
 * 凭空断言「腾讯没有限售解禁」比编一个假数字更难被发现: 用户无从查证一个
 * 声称不存在的东西 (CONTRIBUTING §13)。
 *
 * 九道闸:
 *  1. 无源 ⇒ 渲染后端下发的 message, **绝不**出现「暂无…记录」
 *  2. 无源 ⇒ 收起十个 tab (十份相同说明是噪音, 且每次点击都发一次注定同结果的请求)
 *  3. 无源 ⇒ 收起刷新按钮 (刷新永远不会成功, 留着就是骗用户点)
 *  4. 无源 ⇒ 不渲染 warning 降级横幅 (这是稳定事实, 不是故障, 不该看起来像出错)
 *  5. 无源 ⇒ 不渲染页脚「数据来源」(否则被读成「刚从东财取到 0 条」)
 *  6. 无源文案**由后端下发**, 前端不硬编码 (两处各写一份 ⇒ 将来接入港股源时只改一处)
 *  7. A 股 ok + 有行 ⇒ 表格 / tab / 刷新按钮全在 (零回归)
 *  8. A 股 ok + 0 行 ⇒ 既有空态文案逐字不变
 *  9. A 股 source_unavailable ⇒ warning 横幅 **且刷新按钮仍在**
 *     (可重试语义与结构性无源**相反**, 这条是对照组: 证明第 3 条不是顺手删按钮)
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type StockProfileResponse } from '@/lib/api'
import { StockProfilePanel } from './StockProfilePanel'

const HK_SYMBOL = '00700.HK'
const CN_SYMBOL = '600519.SH'
const REFRESH_TITLE = '跳过缓存强制重拉'
/** 后端 `_MARKET_UNSUPPORTED_MESSAGE` 的首句, 用于断言「文案确实来自后端」 */
const HK_MESSAGE = '个股资料的十类数据集(解禁/质押/回购/股东户数/十大股东/基金持仓/新股申购/业绩快报/资金流向/逐笔成交)均取自 A 股专属披露口径'
/**
 * 🔴 后端**空态**分支会渲染的那句话。它的意思是「我们查过了, 确实没有」——
 * 对港股是假的。整份文件就是为了保证这句话在无源时永不出现。
 */
const FABRICATED_EMPTY = '该标的暂无限售解禁记录'

let host: HTMLDivElement
let root: Root
let client: QueryClient

function resp(partial: Partial<StockProfileResponse> & { state: StockProfileResponse['state'] }): StockProfileResponse {
	return {
		source: 'eastmoney', dataset: 'unlock', symbol: CN_SYMBOL, count: 0, rows: [],
		...partial,
	} as StockProfileResponse
}

beforeEach(() => {
	// 🔴 必须逐文件设置: 本仓**没有**全局 setup 兜底 (vitest.setup.ts 不设它),
	// 漏了会让每条用例都打 "not configured to support act(...)" 警告。
	Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
	host = document.createElement('div')
	document.body.appendChild(host)
	root = createRoot(host)
	// retry:false —— 否则失败路径要等完指数退避; staleTime:Infinity —— 面板自身
	// 按 dataset 设了 10min staleTime, 但换 symbol 重渲染时不该复用上一条结果。
	client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } })
})

afterEach(async () => {
	await act(async () => root.unmount())
	client.clear()
	host.remove()
	vi.restoreAllMocks()
})

async function mount(symbol: string, response: StockProfileResponse) {
	vi.spyOn(api, 'stockProfile').mockResolvedValue(response)
	await act(async () => {
		root.render(
			<QueryClientProvider client={client}>
				<StockProfilePanel symbol={symbol} />
			</QueryClientProvider>,
		)
	})
	// 🔴 宏任务而非 `await Promise.resolve()`: react-query 的通知是批处理的,
	// 只flush微任务会偶发拿不到渲染结果(全量并发跑时尤其明显)。
	await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

const refreshBtn = () => host.querySelector<HTMLButtonElement>(`button[title="${REFRESH_TITLE}"]`)
const tabLabels = ['限售解禁', '股权质押', '股票回购', '股东户数', '十大股东', '基金持仓', '新股申购', '业绩快报', '资金流向', '逐笔成交']

// ── 1. 无源 ⇒ 后端 message, 绝不「暂无…记录」 ─────────────────────────────
it('港股无源: 渲染后端说明, 绝不落到「暂无记录」空态', async () => {
	await mount(HK_SYMBOL, resp({ state: 'market_unsupported', symbol: HK_SYMBOL, message: HK_MESSAGE }))
	const box = host.querySelector('[data-testid="profile-market-unsupported"]')
	expect(box).not.toBeNull()
	expect(host.textContent).toContain(HK_MESSAGE)
	// 🔴 核心断言: 这句话意味着「查过了确实没有」, 对港股是假的
	expect(host.textContent).not.toContain(FABRICATED_EMPTY)
	// 空态模板是 `该标的暂无${label}记录`, 「暂无」是它的签名 ⇒ 一条更宽的兜底
	expect(host.textContent).not.toContain('暂无')
})

// ── 2. 无源 ⇒ 收起 tab ────────────────────────────────────────────────────
it('港股无源: 收起十个数据集 tab', async () => {
	await mount(HK_SYMBOL, resp({ state: 'market_unsupported', symbol: HK_SYMBOL, message: HK_MESSAGE }))
	// 🔴 断言 **DOM 结构**而不是文案子串: 后端 message 本身就会列举十个数据集名
	// (告诉用户到底什么不可用), 用 `not.toContain('股东户数')` 会与文案自相矛盾。
	// tab 与刷新按钮都是 <button>, 无源态下面板里一个按钮都不该剩。
	expect(host.querySelectorAll('button')).toHaveLength(0)
	// 头部只剩标题一个子节点: 控件组是**不渲染**, 不是渲染一个空 div 占位。
	// 🔴 不能写 `.justify-between > div:last-child` 判空 —— 只有一个子节点时它
	// 既是 first-child 也是 last-child, 会匹配到标题本身, 断言恒失败。
	const header = host.querySelector('.justify-between')
	expect(header?.children).toHaveLength(1)
	expect(header?.textContent).toContain('个股资料')
})

// ── 3. 无源 ⇒ 收起刷新按钮 ────────────────────────────────────────────────
it('港股无源: 收起刷新按钮(重试注定失败, 留着就是骗用户点)', async () => {
	await mount(HK_SYMBOL, resp({ state: 'market_unsupported', symbol: HK_SYMBOL, message: HK_MESSAGE }))
	expect(refreshBtn()).toBeNull()
})

// ── 4. 无源 ⇒ 不是 warning ────────────────────────────────────────────────
it('港股无源: 用中性色而非 warning 降级横幅(稳定事实不是故障)', async () => {
	await mount(HK_SYMBOL, resp({ state: 'market_unsupported', symbol: HK_SYMBOL, message: HK_MESSAGE }))
	expect(host.innerHTML).not.toContain('border-warning')
	expect(host.innerHTML).not.toContain('text-warning')
})

// ── 5. 无源 ⇒ 不渲染页脚 ──────────────────────────────────────────────────
it('港股无源: 有 source 也不渲染页脚(否则被读成「刚从东财取到 0 条」)', async () => {
	// 🔴 机制: 页脚的条件是 `fetched_at != null`, 而后端 `_market_unsupported`
	// **刻意不给** `fetched_at`(只给名义 `source`)。所以这里必须保留 source 才能
	// 证明「有源标识也不会误渲染页脚」—— 若把 source 一起删掉, 这条测试就退化成
	// 什么也没验证。fetched_at 的缺失由后端测试钉住, 两侧各管一半。
	await mount(HK_SYMBOL, resp({
		state: 'market_unsupported', symbol: HK_SYMBOL, message: HK_MESSAGE,
		source: 'eastmoney', count: 0,
	}))
	expect(host.textContent).not.toContain('数据来源')
	expect(host.textContent).not.toContain('更新于')
	expect(host.textContent).not.toContain('东方财富')
})

// ── 6. 文案由后端下发 ─────────────────────────────────────────────────────
it('无源文案由后端下发, 前端不硬编码(避免将来接入港股源时只改一处)', async () => {
	const custom = '后端自定义的无源说明 XYZZY'
	await mount(HK_SYMBOL, resp({ state: 'market_unsupported', symbol: HK_SYMBOL, message: custom }))
	expect(host.textContent).toContain(custom)
	expect(host.textContent).not.toContain(HK_MESSAGE)
})

// ── 7. A 股 ok + 有行 ⇒ 零回归 ────────────────────────────────────────────
it('A 股有数据: 表格 / tab / 刷新按钮全部照常渲染', async () => {
	await mount(CN_SYMBOL, resp({
		state: 'ok', fetched_at: 1_795_327_494, stale: false, count: 1,
		rows: [{
			free_date: '2026-12-01', free_shares_wan: 1234, free_market_cap_wan: 5678,
			free_shares_type: '股权激励限售股份', ratio_of_total_pct: 0.13, holder_num: 3,
		}],
	}))
	expect(host.querySelector('[data-testid="profile-market-unsupported"]')).toBeNull()
	// 表头来自 COLUMNS.unlock
	expect(host.textContent).toContain('解禁日期')
	expect(host.textContent).toContain('2026-12-01')
	expect(host.textContent).toContain('股权激励限售股份')
	// tab 全在
	for (const label of tabLabels) expect(host.textContent).toContain(label)
	expect(refreshBtn()).not.toBeNull()
	// 页脚在(fetched_at 非空)
	expect(host.textContent).toContain('数据来源')
	expect(host.textContent).toContain('共 1 条')
})

// ── 8. A 股 ok + 0 行 ⇒ 既有空态逐字不变 ──────────────────────────────────
it('A 股查到 0 条: 既有空态文案不变(与「无源」是两件事)', async () => {
	await mount(CN_SYMBOL, resp({ state: 'ok', fetched_at: 1_795_327_494, count: 0, rows: [] }))
	expect(host.textContent).toContain('该标的暂无限售解禁记录')
	expect(host.querySelector('[data-testid="profile-market-unsupported"]')).toBeNull()
})

// ── 9. 对照组: source_unavailable 仍给刷新按钮 ────────────────────────────
it('A 股源暂不可用: warning 横幅 + 刷新按钮仍在(可重试语义与无源相反)', async () => {
	await mount(CN_SYMBOL, resp({
		state: 'source_unavailable', message: '东方财富资料接口暂不可用: boom',
	}))
	expect(host.innerHTML).toContain('border-warning')
	expect(host.textContent).toContain('东方财富资料接口暂不可用')
	// 🔴 这条是第 3 条的对照组: 收起刷新按钮是**因为无源不可重试**,
	// 不是顺手把按钮删了。源暂时挂掉时刷新恰恰是用户唯一能做的事。
	expect(refreshBtn()).not.toBeNull()
	expect(host.textContent).toContain('暂无数据')
})
