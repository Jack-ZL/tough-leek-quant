// 主题管理 — 暗色(默认) / 亮色切换
//
// 机制:
//   - 状态存 localStorage('tf-theme'), 默认 dark (保持老用户体验不变)
//   - 手动覆盖存 localStorage('tf-theme-override'), 格式 {theme, expiresAt}, 24h 过期
//   - 无手动覆盖(或已过期)时, 按时间自动切换: 7:00-19:00 亮色, 19:00-7:00 暗色
//   - 生效方式: html.dark class (index.css 的 CSS variables + Tailwind darkMode:class)
//   - index.html 里有预渲染内联脚本, 首屏前就设好 class, 避免闪烁 (FOUC)
//   - UI token (bg-surface/text-foreground 等) 自动跟随;
//     图表画布不吃 CSS 变量, 统一走 useChartTheme() 取调色板
import { useEffect, useState } from 'react'

const KEY = 'tf-theme'
const OVERRIDE_KEY = 'tf-theme-override'
const EVENT = 'tf-theme-change'
const OVERRIDE_TTL_MS = 24 * 60 * 60 * 1000 // 24 小时

export type Theme = 'dark' | 'light'

interface OverrideEntry {
  theme: Theme
  expiresAt: number // 时间戳, 过期后自动清除
}

/** 根据时间自动判断主题: 7:00-19:00 亮色, 其余暗色 */
function getAutoTheme(): Theme {
  const h = new Date().getHours()
  return h >= 7 && h < 19 ? 'light' : 'dark'
}

/** 读取覆盖值, 过期则自动清除 */
function readOverride(): OverrideEntry | null {
  try {
    const raw = localStorage.getItem(OVERRIDE_KEY)
    if (!raw) return null
    const entry: OverrideEntry = JSON.parse(raw)
    if (Date.now() > entry.expiresAt) {
      localStorage.removeItem(OVERRIDE_KEY)
      return null
    }
    return entry
  } catch {
    // 旧格式(纯字符串)兼容: 直接清除
    localStorage.removeItem(OVERRIDE_KEY)
    return null
  }
}

/** 是否有未过期的手动覆盖 */
function hasOverride(): boolean {
  return readOverride() !== null
}

/** 获取手动覆盖的主题(未过期时) */
function getOverrideTheme(): Theme | null {
  const entry = readOverride()
  return entry?.theme ?? null
}

export function getTheme(): Theme {
  // 优先手动覆盖
  if (hasOverride()) {
    return getOverrideTheme() ?? getAutoTheme()
  }
  // 无覆盖则自动
  return getAutoTheme()
}

export function setTheme(theme: Theme) {
  try {
    localStorage.setItem(KEY, theme)
  } catch {
    /* ignore */
  }
  document.documentElement.classList.toggle('dark', theme === 'dark')
  window.dispatchEvent(new CustomEvent(EVENT, { detail: theme }))
}

/** 手动切换主题 — 写入覆盖值, 24 小时后自动恢复 */
export function toggleTheme(): Theme {
  const current = getTheme()
  const next: Theme = current === 'dark' ? 'light' : 'dark'
  try {
    const entry: OverrideEntry = {
      theme: next,
      expiresAt: Date.now() + OVERRIDE_TTL_MS,
    }
    localStorage.setItem(OVERRIDE_KEY, JSON.stringify(entry))
  } catch {
    /* ignore */
  }
  setTheme(next)
  return next
}

/** 清除手动覆盖, 恢复自动切换 */
export function clearThemeOverride() {
  try {
    localStorage.removeItem(OVERRIDE_KEY)
  } catch {
    /* ignore */
  }
  const next = getAutoTheme()
  setTheme(next)
}

/** 订阅当前主题 (本页切换 + 其他标签页切换均同步)。 */
export function useTheme(): Theme {
  const [theme, set] = useState<Theme>(getTheme)
  useEffect(() => {
    const onChange = () => set(getTheme())
    window.addEventListener(EVENT, onChange)
    window.addEventListener('storage', onChange) // 跨标签页同步

    // 无手动覆盖时, 每分钟检查一次是否需要自动切换
    const id = setInterval(() => {
      if (!hasOverride()) {
        const next = getAutoTheme()
        if (next !== getTheme()) {
          setTheme(next)
        }
      }
    }, 60_000)

    return () => {
      window.removeEventListener(EVENT, onChange)
      window.removeEventListener('storage', onChange)
      clearInterval(id)
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
