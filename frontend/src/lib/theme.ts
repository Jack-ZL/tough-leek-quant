// 主题管理 — 自动(跟随系统) / 暗色 / 亮色切换
//
// 机制:
//   - 默认 auto: 跟随系统 prefers-color-scheme, 系统切换时实时生效
//   - 手动切换 (dark/light) 为 24h 临时覆盖, 到期后自动恢复到 auto
//   - 状态存 localStorage: 'tf-theme' 存模式 (auto|dark|light),
//     'tf-theme-manual-at' 存最近一次手动切换的时间戳 (ms)
//   - 生效方式: html.dark class (index.css 的 CSS variables + Tailwind darkMode:class)
//   - index.html 里有预渲染内联脚本, 首屏前就解析好 class, 避免闪烁 (FOUC)
//   - UI token (bg-surface/text-foreground 等) 自动跟随;
//     图表画布不吃 CSS 变量, 统一走 useChartTheme() 取调色板
import { useEffect, useState } from 'react'

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

function applyTheme(theme: Theme) {
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

/** 手动切换主题: 写入模式并记录时间, 24h 后 getTheme() 自动回落 auto。 */
export function setTheme(theme: Theme) {
  try {
    localStorage.setItem(KEY, theme)
    localStorage.setItem(AT_KEY, String(Date.now()))
  } catch { /* ignore */ }
  applyTheme(theme)
  window.dispatchEvent(new CustomEvent(EVENT, { detail: theme }))
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
    const sync = () => {
      const next = getTheme()
      applyTheme(next)
      set(next)
    }
    window.addEventListener(EVENT, sync)
    window.addEventListener('storage', sync)  // 跨标签页同步
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

    return () => {
      window.removeEventListener(EVENT, sync)
      window.removeEventListener('storage', sync)
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
