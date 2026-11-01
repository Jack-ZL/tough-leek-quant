// P1-12 layer3: 备源重跑面板 — 批量任务(日K/除权因子)有标的硬失败时,
// 数据页顶部出现本卡片: 展示失败数量/主源/时间, 提供「用备源重跑」与「忽略」。
// 重跑走后台 job (与修正数据同一套进度卡片), 仍失败的标的会重新记回清单。
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Loader2, RotateCcw, X } from 'lucide-react'
import { api, type RerunDataset, type RerunDatasetEntry } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const DATASET_LABELS: Record<RerunDataset, string> = {
  daily: '日K',
  adj_factor: '除权因子',
}

export function BackupRerunPanel({ isRunning, onStart }: {
  isRunning: boolean
  onStart: (jobId: string) => void
}) {
  const qc = useQueryClient()
  const manifest = useQuery({
    queryKey: QK.rerunManifest,
    queryFn: () => api.rerunManifest(),
    refetchInterval: 60_000, // 盘后管道跑完后 1 分钟内自动亮出标记
  })

  const rerun = useMutation({
    mutationFn: (dataset: RerunDataset) => api.rerunWithBackup(dataset),
    onSuccess: (resp) => {
      if (resp.job_id) onStart(resp.job_id)
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      qc.invalidateQueries({ queryKey: QK.rerunManifest })
    },
  })
  const ignore = useMutation({
    mutationFn: (dataset: RerunDataset) => api.clearRerunManifest(dataset),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.rerunManifest }),
  })

  const entries = Object.entries(manifest.data?.datasets ?? {}) as
    [RerunDataset, RerunDatasetEntry][]
  if (!entries.length) return null

  return (
    <div className="space-y-2">
      {entries.map(([dataset, entry]) => (
        <div key={dataset}
          className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-card border border-warning/30 bg-warning/8 px-3 py-2">
          <AlertTriangle className="h-4 w-4 shrink-0 text-warning" />
          <span className="text-xs text-foreground leading-relaxed">
            {DATASET_LABELS[dataset]}同步有 <b className="font-mono">{entry.count}</b> 只标的失败
            <span className="ml-1 text-muted">
              (主源 {entry.source ?? '未知'}{entry.recorded_at ? ` · ${entry.recorded_at}` : ''})
            </span>
            <span className="ml-2 font-mono text-[10px] text-muted">
              {entry.symbols_sample.slice(0, 5).join(', ')}
              {entry.count > 5 ? ' …' : ''}
            </span>
          </span>
          <div className="ml-auto flex items-center gap-2">
            {entry.backup ? (
              <button
                className="flex items-center gap-1 rounded-btn border border-accent/40 bg-accent/10 px-2.5 py-1 text-xs text-accent hover:bg-accent/20 disabled:opacity-50"
                disabled={isRunning || rerun.isPending}
                onClick={() => rerun.mutate(dataset)}
                title={`全程只用备源 ${entry.backup} 重拉失败标的, 不混源`}
              >
                {rerun.isPending && rerun.variables === dataset
                  ? <Loader2 className="h-3 w-3 animate-spin" />
                  : <RotateCcw className="h-3 w-3" />}
                用备源 {entry.backup} 重跑
              </button>
            ) : (
              <span className="text-[10px] text-muted">无可用备源 (需在数据源设置启用免费内置源)</span>
            )}
            <button
              className="flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[10px] text-muted hover:text-foreground disabled:opacity-50"
              disabled={ignore.isPending}
              onClick={() => ignore.mutate(dataset)}
              title="清掉标记, 不重跑 (数据缺口仍在, 可用「日K自检与修补」全量修复)"
            >
              <X className="h-3 w-3" />
              忽略
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}
