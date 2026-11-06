import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, Languages, RefreshCw, Table2 } from 'lucide-react'
import { api, type TdxCompatibilityRow, type TdxTemplate } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 被拒函数的归类 → 中文标签 (后端 builtins.compatibility_table 的 category 取值) */
const REJECT_CATEGORY: Record<string, string> = {
  future: '未来函数',
  perf: '无法向量化',
  data: '缺数据',
  drawing: '画线类',
}

const SUMMARY_CLS = 'flex cursor-pointer select-none items-center gap-1 font-medium text-foreground transition-colors hover:text-accent [&::-webkit-details-marker]:hidden'
const BOX_CLS = 'rounded-btn border border-border/60 bg-base/40 px-2.5 py-1.5 text-[10px]'

interface TdxPanelProps {
  /** 校验响应里的翻译预览 (等价 DSL 文本); 空串 = 还没有可展示的译文 */
  dslText: string
  /** 点模板 → 整体替换编辑器公式; 是否二次确认由父组件决定 */
  onApplyTemplate: (formula: string) => void
}

/** 通达信档专属辅助面板 (P1-17 第 4 批): 公式模板 + 翻译预览 + 函数兼容表。
 *
 *  只在语言档位为「通达信」时挂载 —— DSL/Python 档写的是可执行原文, 不存在
 *  「翻译成什么」的问题, 兼容表也无意义。数据全部来自 GET /api/factors/tdx/meta,
 *  前端不硬编码任何函数名或模板, 后端加一个映射这里自动多一行。 */
export function TdxPanel({ dslText, onApplyTemplate }: TdxPanelProps) {
  const [filter, setFilter] = useState('')
  const meta = useQuery({
    queryKey: QK.factorTdxMeta,
    queryFn: api.factorTdxMeta,
    // 纯静态元数据: 一次会话内不必重取
    staleTime: 600_000,
  })

  const compatibility = meta.data?.compatibility ?? []
  const templates = meta.data?.templates ?? []

  // 模板按 category 分组 (趋势/摆动/通道/均线), 保持后端下发顺序
  const templateGroups = useMemo(() => {
    const groups: { category: string; items: TdxTemplate[] }[] = []
    for (const item of templates) {
      const found = groups.find(g => g.category === item.category)
      if (found) found.items.push(item)
      else groups.push({ category: item.category, items: [item] })
    }
    return groups
  }, [templates])

  // 兼容表按 kind 三分区; 有关键词时强制展开, 否则命中项会被折叠藏起来
  const sections = useMemo(() => {
    const keyword = filter.trim().toLowerCase()
    const hit = (row: TdxCompatibilityRow) =>
      !keyword ||
      row.tdx.toLowerCase().includes(keyword) ||
      (row.maps_to ?? '').toLowerCase().includes(keyword) ||
      (row.reason ?? '').toLowerCase().includes(keyword)
    return [
      { key: 'operator', label: '直接支持', note: '一对一映射到 DSL 算子', rows: compatibility.filter(r => r.kind === 'operator' && hit(r)) },
      { key: 'macro', label: '宏展开', note: '用已有算子组合等价展开', rows: compatibility.filter(r => r.kind === 'macro' && hit(r)) },
      { key: 'rejected', label: '不支持', note: '明确拒绝, 校验会报错并说明原因', rows: compatibility.filter(r => r.kind === 'rejected' && hit(r)) },
    ]
  }, [compatibility, filter])

  const matched = sections.reduce((n, s) => n + s.rows.length, 0)

  return (
    <div className="space-y-1.5">
      {/* ── 公式模板: 不会写通达信公式的人从这里起步 ── */}
      <details open className={`group ${BOX_CLS}`}>
        <summary className={SUMMARY_CLS}>
          <ChevronRight className="h-3 w-3 shrink-0 text-accent transition-transform group-open:rotate-90" />
          通达信公式模板{templates.length > 0 && ` (${templates.length})`}
          <span className="font-normal text-muted">· 点击整体替换公式</span>
        </summary>
        {meta.isPending && <div className="mt-1.5 text-muted">加载模板中…</div>}
        {meta.isError && (
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-danger">
            <span>模板加载失败 · {String((meta.error as Error).message)}</span>
            <button
              type="button"
              onClick={() => void meta.refetch()}
              className="inline-flex cursor-pointer items-center gap-1 rounded-btn border border-danger/40 px-1.5 py-0.5 transition-colors hover:bg-danger/10"
            >
              <RefreshCw className="h-3 w-3" />重试
            </button>
          </div>
        )}
        {!meta.isPending && !meta.isError && templates.length === 0 && (
          <div className="mt-1.5 text-muted">后端未下发模板。</div>
        )}
        <div className="mt-1.5 space-y-1.5">
          {templateGroups.map(group => (
            <div key={group.category}>
              <div className="mb-0.5 font-medium text-secondary">{group.category}</div>
              <div className="flex flex-wrap gap-1">
                {group.items.map(item => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => onApplyTemplate(item.formula)}
                    title={`${item.description}\n\n${item.formula}`}
                    className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-btn border border-border bg-base/60 px-1.5 py-0.5 text-[10px] text-secondary transition-colors hover:border-accent/40 hover:text-accent"
                  >
                    {item.name}
                    <span
                      className={`rounded px-1 font-normal ${item.kind === 'signal' ? 'bg-accent/15 text-accent' : 'bg-elevated text-muted'}`}
                      title={item.kind === 'signal' ? '布尔信号: 可直接当选股条件' : '连续值: 可参与打分排序'}
                    >
                      {item.kind === 'signal' ? '信号' : '数值'}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </details>

      {/* ── 翻译预览: 通达信 → DSL 的等价文本, 让用户看见黑盒里发生了什么 ── */}
      <details open className={`group ${BOX_CLS}`}>
        <summary className={SUMMARY_CLS}>
          <ChevronRight className="h-3 w-3 shrink-0 text-accent transition-transform group-open:rotate-90" />
          <Languages className="h-3 w-3 shrink-0 text-accent" />
          翻译预览
          <span className="font-normal text-muted">· 校验后显示等价 DSL</span>
        </summary>
        {dslText ? (
          <>
            <code className="mt-1.5 block break-all rounded-btn border border-border/50 bg-base/60 px-2 py-1.5 font-mono text-[10px] leading-relaxed text-accent">
              {dslText}
            </code>
            <div className="mt-1 text-muted">
              这是后端实际参与计算的表达式; 报错偏移仍指回你写的通达信原文。存盘保存的也是原文。
            </div>
          </>
        ) : (
          <div className="mt-1.5 text-muted">
            点「校验」后显示。翻译本身失败 (例如用了不支持的函数) 时没有译文, 请看下方兼容表。
          </div>
        )}
      </details>

      {/* ── 函数兼容表: 写之前先查能不能用, 比校验报错后再查省事 ── */}
      <details className={`group ${BOX_CLS}`}>
        <summary className={SUMMARY_CLS}>
          <ChevronRight className="h-3 w-3 shrink-0 text-accent transition-transform group-open:rotate-90" />
          <Table2 className="h-3 w-3 shrink-0 text-accent" />
          函数兼容表{compatibility.length > 0 && ` (${compatibility.length})`}
          <span className="font-normal text-muted">· 支持 / 宏展开 / 不支持</span>
        </summary>
        <div className="mt-1.5">
          <input
            type="text"
            value={filter}
            onChange={event => setFilter(event.target.value)}
            placeholder="筛选函数名 / 映射目标 / 原因…"
            aria-label="筛选通达信函数"
            className="h-7 w-full rounded-input border border-border bg-surface px-2 text-[10px] text-foreground focus:border-accent focus:outline-none"
          />
          {meta.isPending && <div className="mt-1.5 text-muted">加载兼容表中…</div>}
          {!meta.isPending && !meta.isError && compatibility.length === 0 && (
            <div className="mt-1.5 text-muted">后端未下发兼容表。</div>
          )}
          {compatibility.length > 0 && filter.trim() !== '' && (
            <div className="mt-1 text-muted">
              命中 {matched} / {compatibility.length} 个函数
            </div>
          )}
          <div className="mt-1.5 max-h-72 space-y-1.5 overflow-y-auto pr-0.5">
            {sections.map(section => (
              <details key={section.key} open={filter.trim() !== '' || section.key === 'operator'} className="group/inner rounded-btn border border-border/50 bg-base/30 px-2 py-1">
                <summary className={`${SUMMARY_CLS} text-[10px]`}>
                  <ChevronRight className="h-3 w-3 shrink-0 text-accent transition-transform group-open/inner:rotate-90" />
                  <span className={section.key === 'rejected' ? 'text-danger' : 'text-bull'}>{section.label}</span>
                  <span className="font-normal text-muted">({section.rows.length}) · {section.note}</span>
                </summary>
                {section.rows.length === 0 ? (
                  <div className="mt-1 text-muted">无匹配项。</div>
                ) : (
                  <div className="mt-1 space-y-0.5">
                    {section.rows.map(row => (
                      <div key={row.tdx} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 leading-4">
                        <code className="w-24 shrink-0 font-mono text-foreground">{row.tdx}</code>
                        {row.kind !== 'rejected' ? (
                          <code className="break-all font-mono text-accent">{row.maps_to}</code>
                        ) : (
                          <>
                            <span className="shrink-0 rounded bg-danger/10 px-1 text-danger">
                              {REJECT_CATEGORY[row.category ?? ''] ?? row.category ?? '不支持'}
                            </span>
                            <span className="text-muted">{row.reason}</span>
                          </>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </details>
            ))}
          </div>
        </div>
      </details>
    </div>
  )
}
