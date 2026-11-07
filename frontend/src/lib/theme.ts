// 主题管理 — 自动(跟随系统) / 暗色 / 亮色切换
//
// 机制:
//   - 默认 auto: 跟随系统 prefers-color-scheme, 系统切换时实时生效
//   - 手动切换 (dark/light) 为 24h 临时覆盖, 到期后自动恢复到 auto
//   - 状态存 localStorage: 'tf-theme' 存模式 (auto|dark|light),
//     'tf-theme-manual-at' 存最近一次手动切换的时间戳 (ms)
//   - 'tf-theme' 另有后端偏好副本 (uiPrefs, REMOTE_FIRST): 桌面版端口递增会换
//     origin, localStorage 读不到时由后端回填; 回填值按"用户明确选择"对待,
//     补一个新时间戳进入 24h 覆盖窗口 (见 useTheme 里的 hydrate 回调)
//   - 生效方式: html.dark class (index.css 的 CSS variables + Tailwind darkMode:class)
//   - index.html 里有预渲染内联脚本, 首屏前就解析好 class, 避免闪烁 (FOUC)
//   - UI token (bg-surface/text-foreground 等) 自动跟随;
//     图表画布不吃 CSS 变量, 统一走 useChartTheme() 取调色板
import {useEffect, useState} from 'react'
import {hydrateUiPrefs, setUiPref} from '@/lib/uiPrefs'

const KEY = 'tf-theme'
const AT_KEY = 'tf-theme-manual-at'
const EVENT = 'tf-theme-change'
const MS_24H = 24 * 60 * 60 * 1000

export type Theme = 'dark' | 'light'
type Mode = Theme | 'auto'

function readMode(): Mode {
	try {
		const v = localStorage.getItem(KEY)
		return v === 'dark' || v === 'light' || v === 'auto' ? v : 'auto'
	} catch {
		return 'auto'
	}
}

function readManualAt(): number | null {
	try {
		const v = Number(localStorage.getItem(AT_KEY))
		return Number.isFinite(v) && v > 0 ? v : null
	} catch {
		return null
	}
}

/** 系统当前是否偏好暗色; 无法读取时回落到暗色 (与历史默认一致)。 */
function systemDark(): boolean {
	try {
		return window.matchMedia('(prefers-color-scheme: dark)').matches
	} catch {
		return true
	}
}

/** 只切换 html.dark class。恢复后端偏好时用它, 不会把默认值重新写回后端。 */
export function applyTheme(theme: Theme) {
	document.documentElement.classList.toggle('dark', theme === 'dark')
}

/**
 * 解析生效主题: 未过期的手动覆盖优先, 否则跟随系统 (auto)。
 * 旧版只存了 dark/light 而没有时间戳的情况, 视为已过期并回落 auto。
 */
export function getTheme(): Theme {
	const mode = readMode()
	const at = readManualAt()
	if (mode !== 'auto' && at != null && Date.now() - at < MS_24H) return mode
	return systemDark() ? 'dark' : 'light'
}

/**
 * 手动切换主题: 记录时间戳 (24h 后 getTheme() 自动回落 auto), 写 localStorage
 * 即时缓存 + 后端偏好副本, 再广播事件让本页与跨标签页的 useTheme 订阅者同步。
 *
 * setUiPref 内部已同步写了 localStorage[KEY], 所以这里只补 AT_KEY; 而它**不派发
 * 事件**, 事件必须自己发 —— 否则 Layout 的 useTheme 与图表的 useChartTheme 不会
 * 重渲染 (切换后图标和图表配色都不跟着变)。
 */
export function setTheme(theme: Theme) {
	try {
		localStorage.setItem(AT_KEY, String(Date.now()))
	} catch { /* ignore */
	}
	setUiPref(KEY, theme)
	applyTheme(theme)
	window.dispatchEvent(new CustomEvent(EVENT, {detail: theme}))
}

export function toggleTheme(): Theme {
	const next: Theme = getTheme() === 'dark' ? 'light' : 'dark'
	setTheme(next)
	return next
}

/** 订阅当前生效主题 (本页/跨标签页切换 + 系统偏好变化 + 24h 到期均同步)。 */
export function useTheme(): Theme {
	const [theme, set] = useState<Theme>(getTheme)
	useEffect(() => {
		let active = true
		const sync = () => {
			if (!active) return
			const next = getTheme()
			applyTheme(next)
			set(next)
		}
		window.addEventListener(EVENT, sync)

		// 跨标签页同步。另一个标签页刚做出的 dark/light 选择等同于「此刻的手动切换」,
		// 若它没带 manual-at (旧版页面, 或绕过 setTheme 直接写 localStorage), 在这里补
		// 一个时间戳 —— 否则 getTheme() 会按「旧版遗留」判为过期而回落 auto, 同步不过来。
		// 补戳只能放在 storage 事件里: 放进 sync 会让 24h 到期定时器每次触发都给自己续期,
		// 「到期恢复 auto」就永远不发生了。同 origin 的遗留值不触发 storage 事件, 迁移规则不受影响。
		const onStorage = (e: StorageEvent) => {
			if (!active) return
			if ((e.key === KEY || e.key === null) && readMode() !== 'auto' && readManualAt() == null) {
				try {
					localStorage.setItem(AT_KEY, String(Date.now()))
				} catch { /* ignore */
				}
			}
			sync()
		}
		window.addEventListener('storage', onStorage)

		const mql = window.matchMedia('(prefers-color-scheme: dark)')
		mql.addEventListener('change', sync)      // auto 模式下跟随系统

		// 手动覆盖 24h 后自动恢复: 到期时无需用户操作即重新解析并同步。
		let timer: number | undefined
		const schedule = () => {
			if (timer != null) clearTimeout(timer)
			const mode = readMode()
			const at = readManualAt()
			if (mode !== 'auto' && at != null) {
				timer = window.setTimeout(sync, Math.max(at + MS_24H - Date.now(), 0) + 100)
			}
		}
		schedule()

		// 登录页在同一 SPA 内跳转，首次回填若为 401，挂载后仍须重新恢复主题。
		void hydrateUiPrefs().then(() => {
			if (!active) return
			// 后端回填的主题是用户在别的 origin/会话里做出的明确选择, 补一个时间戳让它
			// 进入 24h 覆盖窗口; 否则 getTheme() 会因缺 manual-at 判为「旧版已过期」而
			// 回落 auto —— 桌面版换端口重启后主题就保不住 (AT_KEY 只存 localStorage,
			// 不随后端偏好走)。同 origin 的遗留值不经回填, 仍按过期处理。
			if (readMode() !== 'auto' && readManualAt() == null) {
				try {
					localStorage.setItem(AT_KEY, String(Date.now()))
				} catch { /* ignore */
				}
			}
			sync()
			schedule()  // 回填可能改变了 mode, 到期定时器要重排
		})

		return () => {
			active = false
			window.removeEventListener(EVENT, sync)
			window.removeEventListener('storage', onStorage)
			mql.removeEventListener('change', sync)
			if (timer != null) clearTimeout(timer)
		}
	}, [])
	return theme
}

// ================================================================
// 图表调色板 — ECharts / lightweight-charts 画布不吃 CSS 变量,
// 所有图表组件统一从这里取色, 主题切换时依赖 useTheme 重建 option。
// bull/bear/accent 等语义色双主题一致, 不在此重复定义。
// ================================================================

export interface ChartTheme {
	/** 轴刻度/图例等常规文字 */
	text: string
	/** 信息条/图例里的强调文字 */
	textStrong: string
	/** 网格线 */
	grid: string
	/** 轴线/边框 */
	border: string
	/** 十字光标线 */
	crosshair: string
	/** 十字光标轴标签背景 */
	crosshairLabelBg: string
	/** tooltip 背景 */
	tooltipBg: string
	/** tooltip 边框 */
	tooltipBorder: string
	/** tooltip 文字 */
	tooltipText: string
	/** 半透明信息条背景 (K线图左上角 OHLC 条) */
	infoBarBg: string
	/** dataZoom 滑块填充 */
	zoomFill: string
	/** 分时图均价线以外的弱填充 */
	fillSubtle: string
}

const DARK: ChartTheme = {
	text: '#A1A1AA',
	textStrong: '#E4E4E7',
	grid: 'rgba(255,255,255,0.06)',
	border: '#27272A',
	crosshair: 'rgba(255,255,255,0.25)',
	crosshairLabelBg: '#333',
	tooltipBg: 'rgba(24,24,27,0.95)',
	tooltipBorder: 'rgba(255,255,255,0.1)',
	tooltipText: '#E4E4E7',
	infoBarBg: 'rgba(39,39,42,0.6)',
	zoomFill: 'rgba(255,255,255,0.06)',
	fillSubtle: 'rgba(255,255,255,0.04)',
}

const LIGHT: ChartTheme = {
	text: '#71717A',
	textStrong: '#27272A',
	grid: 'rgba(0,0,0,0.06)',
	border: '#E4E4E7',
	crosshair: 'rgba(0,0,0,0.3)',
	crosshairLabelBg: '#52525B',
	tooltipBg: 'rgba(255,255,255,0.97)',
	tooltipBorder: 'rgba(0,0,0,0.1)',
	tooltipText: '#27272A',
	infoBarBg: 'rgba(244,244,245,0.85)',
	zoomFill: 'rgba(0,0,0,0.06)',
	fillSubtle: 'rgba(0,0,0,0.04)',
}

export function chartTheme(theme: Theme): ChartTheme {
	return theme === 'dark' ? DARK : LIGHT
}

/** hook: 当前主题的图表调色板 (主题切换自动触发重渲染)。 */
export function useChartTheme(): ChartTheme {
	return chartTheme(useTheme())
}
