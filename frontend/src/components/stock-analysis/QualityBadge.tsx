// P1-11 数据体检徽章 —— 个股分析 / 多空辩论对话框共用。
// 等级配色: A 绿 / B 蓝 / C 黄 / D 红; 点击展开问题清单 (info 级不展示, 只作提示语)。
import { useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/cn'
import type { DataQuality } from '@/lib/api'

const QUALITY_TONE: Record<DataQuality['grade'], string> = {
	A: 'border-bull/30 bg-bull/10 text-bull',
	B: 'border-sky-400/30 bg-sky-500/10 text-sky-300',
	C: 'border-amber-400/30 bg-amber-500/10 text-amber-300',
	D: 'border-bear/30 bg-bear/10 text-bear',
}

const SEVERITY_LABEL: Record<string, string> = {
	high: '严重', medium: '中等', low: '轻微', info: '提示',
}

export function QualityBadge({ quality }: { quality: DataQuality }) {
	const [open, setOpen] = useState(false)
	// info 级只是说明 (如"暂无财务数据"), 不算问题, 不进清单
	const issues = quality.issues.filter(i => i.severity !== 'info')
	return (
		<div className="mb-3">
			<button
				onClick={() => issues.length > 0 && setOpen(v => !v)}
				className={cn(
					'flex w-full items-center gap-2 rounded-lg border px-3 py-1.5 text-xs transition-colors',
					QUALITY_TONE[quality.grade] ?? 'border-border/40 bg-elevated/20 text-muted',
					issues.length > 0 && 'hover:brightness-110',
				)}
				title={issues.length > 0
					? `点击${open ? '收起' : '展开'}问题清单 (体检分 ${quality.score}/100)`
					: `数据体检通过, 无问题 (${quality.score}/100)`}
			>
				<span className="shrink-0 opacity-80">🩺 数据体检</span>
				<span className="font-semibold shrink-0">{quality.grade} 级</span>
				<span className="shrink-0 opacity-80">{quality.score}/100</span>
				{quality.hard && (
					<span className="shrink-0 rounded bg-bear/20 px-1.5 py-0.5 text-[10px] text-bear">含硬伤</span>
				)}
				{issues.length === 0 ? (
					<span className="truncate opacity-70">数据健康, AI 可放心使用</span>
				) : (
					<span className="truncate opacity-80">{issues.length} 项问题, AI 已被告知并降低置信度</span>
				)}
				{issues.length > 0 && (
					<ChevronDown className={cn('ml-auto h-3.5 w-3.5 shrink-0 transition-transform', open && 'rotate-180')} />
				)}
			</button>
			{open && issues.length > 0 && (
				<ul className="mt-1 space-y-1 rounded-lg border border-border/40 bg-elevated/20 px-3 py-2 text-xs text-secondary">
					{issues.map((it, i) => (
						<li key={`${it.code}-${i}`} className="flex items-start gap-2">
							<span className={cn(
								'shrink-0 rounded px-1 py-0.5 text-[10px] leading-none',
								it.severity === 'high' && 'bg-bear/15 text-bear',
								it.severity === 'medium' && 'bg-amber-500/15 text-amber-300',
								it.severity === 'low' && 'bg-sky-500/15 text-sky-300',
							)}>{SEVERITY_LABEL[it.severity] ?? it.severity}</span>
							<span>{it.message}</span>
						</li>
					))}
				</ul>
			)}
		</div>
	)
}
