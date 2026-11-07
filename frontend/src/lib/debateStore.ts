import { useSyncExternalStore } from 'react'
import {
	api, friendlyStreamError,
	type AiVerdict, type DataQuality, type DebateReport, type DebateRole, type LevelType, type PriceLevel,
} from './api'

/**
 * 多空辩论 (P1-9) —— 全局单场 store。
 *
 * 与 stockAnalysisStore 的区别: 同时只允许一场辩论 (一次 3~5 次 LLM 调用,
 * 成本高于普通分析, 不做多任务并发); 流式接收逻辑在此, 与弹窗解耦 →
 * 关闭弹窗后台照常累积, 重新打开能看到最新进度。
 *
 * 事件协议 (后端 stock_debate.debate_stream):
 *   meta → role_delta(bull/bear 交错) → role_done ×2 → role_delta(judge)…
 *   → role_done(judge) → ruling(初审) → [rewrite → 重写流 → ruling(终审)] → done
 */

export type DebatePhase = 'streaming' | 'done' | 'error'

export interface DebateState {
	open: boolean
	symbol: string
	name: string
	phase: DebatePhase | null
	/** 三方文本: 流式累积; role_done 时用最终全文覆盖 (重写过的为重写版) */
	texts: Record<DebateRole, string>
	/** 各角色是否正在输出 (驱动打字光标) */
	streaming: Record<DebateRole, boolean>
	/** 各角色是否被打回重写过 (栏顶展示"已重写"徽标) */
	rewritten: Record<DebateRole, boolean>
	/** 打回理由 (rewrite 事件下发, 展示在被重写方栏顶) */
	rewriteReason: string
	/** meta 事件: 各角色实际使用的模型 */
	roles: Partial<Record<DebateRole, { model: string; custom_profile: boolean }>>
	meta: {
		summary?: string
		levels?: Record<LevelType, PriceLevel[]>
		close?: number | null
		/** 数据体检 (P1-11): meta 事件下发, 徽章展示 (辩论报告暂不落库该字段) */
		quality?: DataQuality | null
		/** P1-15 模型归属: meta 事件下发, 保存辩论记录时原样带回落盘 */
		model?: string
		provider?: string
		profile_id?: string
	} | null
	/** 生效裁决: 终审 ruling 优先, 无重写时取初审 ruling */
	ruling: { ok: boolean; decision?: 'pass' | 'rewrite'; verdict?: AiVerdict | null; final?: boolean } | null
	error: string
	/** 单方辩手降级警告 (role_error 事件): 非致命, 辩论继续; 双方都失败才走 error */
	warning: string
	/** 非 null = 正在查看历史记录 (覆盖实时视图) */
	viewing: DebateReport | null
	history: DebateReport[]
	historyOpen: boolean
	savedReportId?: string
}

const INITIAL: DebateState = {
	open: false, symbol: '', name: '', phase: null,
	texts: {bull: '', bear: '', judge: ''},
	streaming: {bull: false, bear: false, judge: false},
	rewritten: {bull: false, bear: false, judge: false},
	rewriteReason: '',
	roles: {}, meta: null, ruling: null, error: '', warning: '',
	viewing: null, history: [], historyOpen: false,
}

let state: DebateState = {...INITIAL}
const listeners = new Set<() => void>()

function emit() {
	listeners.forEach(fn => fn())
}

function subscribe(fn: () => void) {
	listeners.add(fn);
	return () => {
		listeners.delete(fn)
	}
}

function patch(p: Partial<DebateState>) {
	state = {...state, ...p};
	emit()
}

export function useDebateState(): DebateState {
	return useSyncExternalStore(subscribe, () => state, () => INITIAL)
}

// ===== 动作 =====

export function closeDebate() {
	patch({open: false, historyOpen: false, viewing: null})
}

/** 打开辩论对话框 — 落地在历史列表, 不自动开辩 (看历史零成本)。 */
export function openDebate(symbol: string, name: string) {
	if (state.phase === 'streaming') {
		// 已有辩论在跑: 直接打开看实时进度, 不重置 (单场约束)
		patch({open: true, historyOpen: false, viewing: null})
		return
	}
	patch({
		...INITIAL,
		open: true, symbol, name,
		historyOpen: true,
		history: state.history,
	})
	void loadHistory()
}

/** 发起一场辩论 (对话框内「发起新辩论」按钮调用; 用当前 symbol/name)。已在流式中则忽略。 */
export function startDebate() {
	if (state.phase === 'streaming' || !state.symbol) return
	patch({
		phase: 'streaming',
		historyOpen: false, viewing: null,
		texts: {bull: '', bear: '', judge: ''},
		streaming: {bull: true, bear: true, judge: false},
		rewritten: {bull: false, bear: false, judge: false},
		rewriteReason: '', roles: {}, meta: null, ruling: null, error: '', warning: '',
		savedReportId: undefined,
	})
	void runDebate(state.symbol, state.name)
}

async function runDebate(symbol: string, name: string) {
	// 流式期间的增量在本地累积, role_done 用最终全文覆盖 (防 delta 丢失/重复)
	const texts: Record<DebateRole, string> = {bull: '', bear: '', judge: ''}
	let ruling: DebateState['ruling'] = null
	let roles: DebateState['roles'] = {}
	let meta: DebateState['meta'] = null
	let rewritten = false
	try {
		for await (const ev of api.debateStream(symbol, name)) {
			// 用户已关闭或已开新场 → 丢弃旧流事件 (symbol 校验防串场)
			if (!state.open || state.symbol !== symbol || state.phase !== 'streaming') return
			switch (ev.type) {
				case 'meta':
					roles = ev.roles ?? {}
					// P1-11: quality 随 meta 下发 (体检失败时为 null, 徽章不显示)
					// P1-15: 归属快照一并接住, 保存时带回
					meta = {
						summary: ev.summary,
						levels: ev.levels,
						close: ev.close ?? null,
						quality: ev.quality ?? null,
						model: ev.model,
						provider: ev.provider,
						profile_id: ev.profile_id
					}
					patch({roles, meta, name: ev.name || name})
					break
				case 'role_delta': {
					const role = ev.role
					if (!role) break
					texts[role] += ev.content ?? ''
					patch({
						texts: {...texts},
						streaming: {...state.streaming, [role]: true},
					})
					break
				}
				case 'role_done': {
					const role = ev.role
					if (!role) break
					texts[role] = ev.content ?? texts[role]
					patch({
						texts: {...texts},
						streaming: {...state.streaming, [role]: false},
						rewritten: {...state.rewritten, [role]: !!ev.rewritten},
					})
					break
				}
				case 'rewrite':
					rewritten = true
					if (ev.role) {
						// 清空该角色旧文, 重写增量从零累积
						texts[ev.role] = ''
						patch({
							texts: {...texts},
							streaming: {...state.streaming, [ev.role]: true},
							rewriteReason: ev.reason ?? '',
						})
					}
					break
				case 'ruling':
					// 终审 ruling 覆盖初审; 初审 rewrite 的裁决不生效 (只用于展示打回)
					ruling = {ok: ev.ok ?? false, decision: ev.decision, verdict: ev.verdict ?? null, final: ev.final}
					patch({ruling})
					break
				case 'role_error':
				// 单方辩手失败降级: 只挂警告横幅, phase 保持 streaming 让辩论继续
				patch({
					warning: state.warning
						? `${state.warning}\n${friendlyStreamError(ev.message)}`
						: friendlyStreamError(ev.message),
				})
				break
			case 'error':
					patch({
						phase: 'error',
						error: friendlyStreamError(ev.message),
						streaming: {bull: false, bear: false, judge: false}
					})
					return
				case 'done':
					patch({phase: 'done', streaming: {bull: false, bear: false, judge: false}})
					break
				default:
					break // ping 等忽略
			}
		}
		if (state.phase !== 'done') {
			// 流断了却没收到 done (网络中断等) — 按已有内容收尾, 不假装成功
			patch({
				phase: state.phase === 'streaming' ? 'error' : state.phase,
				error: state.phase === 'streaming' ? '连接中断,内容可能不完整' : state.error
			})
		}
		if (state.phase === 'done') await saveDebate(symbol, name, texts, ruling, roles, meta, rewritten)
	} catch (e) {
		if (state.open && state.symbol === symbol && state.phase === 'streaming') {
			patch({
				phase: 'error',
				error: friendlyStreamError(e instanceof Error ? e.message : String(e)),
				streaming: {bull: false, bear: false, judge: false}
			})
		}
	}
}

/** done 后自动落库 (同 stockAnalysisStore 纪律: 保存失败不打扰, 内容已在界面) */
async function saveDebate(
	symbol: string, name: string,
	texts: Record<DebateRole, string>,
	ruling: DebateState['ruling'],
	roles: DebateState['roles'],
	meta: DebateState['meta'],
	rewritten: boolean,
) {
	try {
		const res = await api.debateReportSave({
			symbol, name, focus: '',
			bull_text: texts.bull, bear_text: texts.bear, judge_text: texts.judge,
			verdict: ruling?.verdict ?? null,
			rewritten,
			roles: Object.fromEntries(
				Object.entries(roles).map(([k, v]) => [k, v?.model ?? ''])) as Partial<Record<DebateRole, string>>,
			summary: meta?.summary ?? '',
			close: meta?.close ?? null,
			levels: meta?.levels,
			// P1-15 模型归属快照落库 (各角色实际模型已在 roles 字段)
			model: meta?.model ?? '',
			provider: meta?.provider ?? '',
			profile_id: meta?.profile_id ?? '',
		})
		patch({savedReportId: res.report?.id})
		if (state.historyOpen) void loadHistory()
	} catch { /* 静默: 记录保存失败不影响本场展示 */
	}
}

// ===== 历史 =====

export async function loadHistory() {
	try {
		const res = await api.debateReportsList()
		patch({history: res.reports ?? []})
	} catch { /* 静默 */
	}
}

export function toggleHistory() {
	const next = !state.historyOpen
	patch({historyOpen: next, viewing: null})
	if (next && state.history.length === 0) void loadHistory()
}

export function viewReport(report: DebateReport) {
	patch({viewing: report})
}

export function backToLive() {
	patch({viewing: null})
}

export async function deleteHistoryReport(id: string) {
	try {
		await api.debateReportDelete(id)
	} catch { /* 静默 */
	}
	if (state.viewing?.id === id) patch({viewing: null})
	void loadHistory()
}
