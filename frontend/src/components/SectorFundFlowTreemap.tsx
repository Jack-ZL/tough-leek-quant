import { useEffect, useMemo, useRef } from 'react'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { useChartTheme } from '@/lib/theme'
import type { SectorFundFlowItem } from '@/lib/api'
import { fmtPct, fmtVolume } from '@/lib/format'

// 每侧展示板块数 (数据源返回前 100, 截取前 N 保证色块可读)
const TOP_N = 20

interface PaneConfig {
  /** 方向标识: 净流入 / 净流出 */
  label: string
  /** 数值最小 (最浅色) */
  light: string
  /** 数值最大 (最深色) */
  deep: string
  /** tooltip 里主力金额的文案前缀 */
  hint: string
}

const CONFIG: Record<'inflow' | 'outflow', PaneConfig> = {
  inflow: { label: '净流入居前', light: '#FECACA', deep: '#DC2626', hint: '主力净流入' },
  outflow: { label: '净流出居前', light: '#BBF7D0', deep: '#16A34A', hint: '主力净流出' },
}

function hexRgb(hex: string): [number, number, number] {
  const h = hex.replace('#', '')
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]
}

/** 在 light → deep 之间按 t∈[0,1] 线性插值 */
function lerpColor(light: string, deep: string, t: number): string {
  const a = hexRgb(light)
  const b = hexRgb(deep)
  const r = Math.round(a[0] + (b[0] - a[0]) * t)
  const g = Math.round(a[1] + (b[1] - a[1]) * t)
  const bl = Math.round(a[2] + (b[2] - a[2]) * t)
  return `rgb(${r},${g},${bl})`
}

interface TreemapDatum {
  name: string
  value: number
  mainNet?: number | null
  changePct?: number | null
}

function TreemapPane({ cfg, items }: { cfg: PaneConfig; items: SectorFundFlowItem[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<echarts.ECharts | null>(null)
  const chartTheme = useChartTheme()

  const option = useMemo<EChartsOption | null>(() => {
    if (!items.length) return null
    const max = Math.max(...items.map(i => Math.abs(i.main_net_inflow ?? 0)))
    const data: TreemapDatum[] = items.map(it => {
      const v = Math.abs(it.main_net_inflow ?? 0)
      const ratio = max > 0 ? v / max : 0
      return {
        name: it.name ?? '—',
        value: v,
        mainNet: it.main_net_inflow,
        changePct: it.change_pct,
        itemStyle: { color: lerpColor(cfg.light, cfg.deep, ratio) },
        label: { color: ratio >= 0.5 ? '#FFFFFF' : '#1F2937' },
      } as TreemapDatum & { itemStyle: unknown; label: unknown }
    })
    return {
      tooltip: {
        backgroundColor: chartTheme.tooltipBg,
        borderColor: chartTheme.tooltipBorder,
        borderWidth: 1,
        textStyle: { color: chartTheme.tooltipText, fontSize: 12 },
        formatter: (p: unknown) => {
          const d = (p as { data?: TreemapDatum }).data
          return [
            `<b>${d?.name ?? '—'}</b>`,
            `${cfg.hint}：${fmtVolume(d?.mainNet ?? null)}`,
            `涨跌幅：${fmtPct(d?.changePct ?? null)}`,
          ].join('<br/>')
        },
      },
      series: [
        {
          type: 'treemap',
          roam: false,
          nodeClick: false,
          breadcrumb: { show: false },
          left: 0,
          top: 0,
          right: 0,
          bottom: 0,
          itemStyle: { borderColor: chartTheme.border, borderWidth: 1, gapWidth: 2 },
          label: {
            show: true,
            fontSize: 13,
            fontWeight: 600,
            lineHeight: 16,
            overflow: 'truncate',
            ellipsis: '…',
            formatter: (p: unknown) => (p as { name?: string }).name ?? '',
          },
          upperLabel: { show: false },
          labelLayout: { hideOverlap: false },
          emphasis: {
            label: { show: true },
            itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.35)' },
          },
          data,
        },
      ],
    }
  }, [items, cfg, chartTheme])

  useEffect(() => {
    if (!ref.current) return
    const leftover = echarts.getInstanceByDom(ref.current)
    if (leftover && leftover !== instRef.current) leftover.dispose()
    if (!instRef.current || instRef.current.isDisposed()) {
      instRef.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    }
    const inst = instRef.current
    if (option) {
      inst.setOption(option, { notMerge: true })
    } else {
      inst.clear()
    }
    inst.resize()
  }, [option])

  useEffect(() => {
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      instRef.current?.dispose()
      instRef.current = null
    }
  }, [])

  return <div ref={ref} className="h-[420px] w-full" />
}

export function SectorFundFlowTreemap({
  inflow,
  outflow,
}: {
  inflow: SectorFundFlowItem[]
  outflow: SectorFundFlowItem[]
}) {
  const inItems = useMemo(() => inflow.slice(0, TOP_N), [inflow])
  const outItems = useMemo(() => outflow.slice(0, TOP_N), [outflow])
  if (!inItems.length && !outItems.length) {
    return <div className="py-3 text-[11px] text-muted">暂无板块资金流向数据</div>
  }
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <div>
        <div className="mb-1.5 flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: CONFIG.inflow.deep }} />
          <span className="text-[11px] font-medium tracking-wide text-muted">{CONFIG.inflow.label}</span>
        </div>
        <TreemapPane cfg={CONFIG.inflow} items={inItems} />
      </div>
      <div>
        <div className="mb-1.5 flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: CONFIG.outflow.deep }} />
          <span className="text-[11px] font-medium tracking-wide text-muted">{CONFIG.outflow.label}</span>
        </div>
        <TreemapPane cfg={CONFIG.outflow} items={outItems} />
      </div>
    </div>
  )
}
