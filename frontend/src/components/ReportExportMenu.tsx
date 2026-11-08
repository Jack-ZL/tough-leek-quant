import { useState } from 'react'
import { Download, FileCode2, FileText, Image as ImageIcon, Printer } from 'lucide-react'
import {
	downloadReportHtml, exportReportPng, printReportPdf,
	type ReportExportInput,
} from '@/lib/reportExport'
import { toast } from '@/components/Toast'

/**
 * 报告导出菜单 (P1-21): HTML 文件 / PDF (浏览器打印) / PNG 图片。
 * 纯前端零依赖, 见 lib/reportExport.ts。两种按钮风格:
 * icon (弹窗头部图标钮, 默认) 与 text (工具栏文字钮, 对齐复盘页样式)。
 */
export function ReportExportMenu({ input, variant = 'icon', onMarkdown }: {
	input: ReportExportInput
	variant?: 'icon' | 'text'
	/** 提供时菜单首项追加「Markdown 源文件」(原始 .md 下载, 如复盘页原下载按钮) */
	onMarkdown?: () => void
}) {
	const [open, setOpen] = useState(false)

	const run = async (fn: () => void | Promise<void>, label: string) => {
		setOpen(false)
		try {
			await fn()
			if (fn !== printReportPdf) toast(`${label}已导出`, 'success')
		} catch (e) {
			toast(`${label}导出失败: ${e instanceof Error ? e.message : e}`, 'error')
		}
	}

	const itemCls = 'flex w-full items-center gap-2 px-3 py-1.5 text-left text-[11px] text-secondary transition-colors hover:bg-elevated hover:text-foreground'
	return (
		<div className="relative shrink-0">
			{open && <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />}
			{variant === 'icon' ? (
				<button onClick={() => setOpen(o => !o)} title="导出报告 (HTML/PDF/图片)"
					className="p-1.5 rounded-lg hover:bg-elevated text-muted hover:text-foreground transition-colors">
					<Download className="h-4 w-4" />
				</button>
			) : (
				<button onClick={() => setOpen(o => !o)} title="导出报告 (HTML/PDF/图片)"
					className="inline-flex items-center gap-1 rounded-btn bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground hover:bg-elevated/70">
					<Download className="h-3 w-3" />导出
				</button>
			)}
			{open && (
				<div className="absolute right-0 top-full z-50 mt-1 w-40 rounded-lg border border-border bg-surface py-1 shadow-2xl">
					{onMarkdown && (
						<button className={itemCls} onClick={() => run(onMarkdown, 'Markdown')}>
							<FileText className="h-3.5 w-3.5" />Markdown 源文件
						</button>
					)}
					<button className={itemCls} onClick={() => run(() => downloadReportHtml(input), 'HTML')}>
						<FileCode2 className="h-3.5 w-3.5" />HTML 文件
					</button>
					<button className={itemCls} onClick={() => run(() => printReportPdf(input), 'PDF')}
						title="打开打印对话框, 目标选「另存为 PDF」">
						<Printer className="h-3.5 w-3.5" />PDF (打印)
					</button>
					<button className={itemCls} onClick={() => run(() => exportReportPng(input), '图片')}>
						<ImageIcon className="h-3.5 w-3.5" />PNG 图片
					</button>
				</div>
			)}
		</div>
	)
}
