/** 报告一键导出 (P1-21) — 纯前端零依赖。
 *
 * 复用 MarkdownRenderer (renderToStaticMarkup) 把报告 markdown 渲染成 HTML,
 * 组装自包含单文件 (内联 CSS, 固定浅色打印友好配色, 不依赖 Tailwind 运行时):
 * - HTML: Blob 直接下载, 存档/转发;
 * - PDF: 隐藏 iframe 加载后调浏览器原生打印 → "另存为 PDF" (中文无字体坑);
 * - PNG: iframe 文档序列化进 SVG foreignObject → canvas → 下载 (报告为纯
 *   文本/表格, 无外部资源, canvas 不会被污染)。
 */
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'

export interface ReportExportInput {
	/** 报告标题 (导出页眉 + 文件名前缀) */
	title: string
	/** 元信息行 (标的/时间/模型等, 逐行显示在标题下) */
	meta?: string[]
	/** 报告正文 markdown */
	markdown: string
	/** 下载文件名 (不含扩展名); 缺省用 title */
	filename?: string
}

function escapeHtml(s: string): string {
	return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

/** 映射 MarkdownRenderer 输出的 Tailwind 类到固定浅色配色 (对齐 index.css 亮色主题)。
 * 只覆盖渲染器实际产出的类; hover/focus 态在导出物里无意义, 不映射。 */
const EXPORT_CSS = `
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: #18181B;
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
    "Microsoft YaHei", "Segoe UI", Roboto, sans-serif; }
.page { max-width: 860px; margin: 0 auto; padding: 32px 40px 24px; }
.report-header { border-bottom: 2px solid #3B82F6; padding-bottom: 12px; margin-bottom: 20px; }
.report-header h1 { margin: 0 0 6px; font-size: 20px; line-height: 1.4; }
.report-meta { font-size: 12px; color: #71717A; line-height: 1.8; }
.report-footer { margin-top: 28px; padding-top: 10px; border-top: 1px solid #E4E4E7;
  font-size: 11px; color: #A1A1AA; }
/* ── MarkdownRenderer 类映射 ── */
.flex { display: flex; } .items-start { align-items: flex-start; }
.items-center { align-items: center; } .justify-center { justify-content: center; }
.gap-2 { gap: 8px; } .gap-1\\.5 { gap: 6px; } .flex-1 { flex: 1 1 0%; }
.shrink-0 { flex-shrink: 0; } .space-y-2 > * + * { margin-top: 8px; }
.text-sm { font-size: 13px; } .text-xs { font-size: 12px; }
.text-\\[16px\\] { font-size: 16px; } .leading-6 { line-height: 24px; }
.leading-relaxed { line-height: 1.65; }
.font-semibold { font-weight: 600; } .font-medium { font-weight: 500; }
.font-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.text-left { text-align: left; } .whitespace-nowrap { white-space: nowrap; }
.break-words { overflow-wrap: break-word; } .underline { text-decoration: underline; }
.underline-offset-2 { text-underline-offset: 2px; } .cursor-help { cursor: help; }
.text-\\[10px\\] { font-size: 10px; } .text-\\[0\\.85em\\] { font-size: 0.85em; }
.my-3 { margin: 12px 0; } .my-4 { margin: 16px 0; } .my-5 { margin: 20px 0; }
.my-6 { margin: 24px 0; } .mt-5 { margin-top: 20px; } .mt-6 { margin-top: 24px; }
.mt-0\\.5 { margin-top: 2px; } .mt-\\[7px\\] { margin-top: 7px; } .mb-3 { margin-bottom: 12px; }
.pl-3 { padding-left: 12px; } .pr-2 { padding-right: 8px; }
.px-1 { padding: 0 4px; } .px-2\\.5 { padding-left: 10px; padding-right: 10px; }
.py-0\\.5 { padding-top: 2px; padding-bottom: 2px; }
.py-1\\.5 { padding-top: 6px; padding-bottom: 6px; }
.h-1 { height: 4px; } .w-1 { width: 4px; } .h-4 { height: 16px; } .w-4 { width: 16px; }
.rounded { border-radius: 4px; } .rounded-r { border-top-right-radius: 4px; border-bottom-right-radius: 4px; }
.rounded-full { border-radius: 9999px; } .rounded-btn { border-radius: 8px; }
.border-b { border-bottom: 1px solid #E4E4E7; } .border-l-2 { border-left: 2px solid #E4E4E7; }
.last\\:border-0:last-child { border: 0; } .align-top { vertical-align: top; }
.w-auto { width: auto; } .overflow-x-auto { overflow-x: auto; } .overflow-hidden { overflow: hidden; }
.text-foreground { color: #18181B; } .text-foreground\\/90 { color: rgba(24,24,27,.9); }
.text-secondary { color: #52525B; } .text-accent { color: #2563EB; }
.text-bull { color: #E03A2F; } .text-bear { color: #0E9F5E; }
.text-amber-500 { color: #D97706; }
.bg-elevated { background: #F4F4F5; } .bg-elevated\\/50 { background: rgba(244,244,245,.5); }
.bg-accent\\/10 { background: rgba(59,130,246,.1); } .bg-accent\\/60 { background: rgba(59,130,246,.6); }
.bg-bull\\/10 { background: rgba(240,68,56,.1); } .bg-bear\\/10 { background: rgba(18,183,106,.1); }
.bg-amber-500\\/15 { background: rgba(245,158,11,.15); }
.bg-amber-400\\/\\[0\\.04\\] { background: rgba(251,191,36,.06); }
.border-border { border-color: #E4E4E7; } .border-border\\/20 { border-color: rgba(228,228,231,.4); }
.border-border\\/30 { border-color: rgba(228,228,231,.6); } .border-border\\/40 { border-color: rgba(228,228,231,.7); }
.border-amber-400\\/40 { border-color: rgba(251,191,36,.5); }
table { border-collapse: collapse; }
@media print { body { background: #fff; } .page { max-width: none; padding: 0 8mm; } }
`

/** 组装自包含单文件 HTML (浅色打印友好, 含页眉元信息与免责页脚)。 */
export function buildReportHtml(input: ReportExportInput): string {
	const contentHtml = renderToStaticMarkup(createElement(MarkdownRenderer, { content: input.markdown }))
	const meta = (input.meta ?? []).filter(Boolean)
		.map(m => `<div>${escapeHtml(m)}</div>`).join('')
	const now = new Date()
	const ts = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`
	return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(input.title)}</title>
<style>${EXPORT_CSS}</style>
</head>
<body>
<div class="page">
<header class="report-header">
<h1>${escapeHtml(input.title)}</h1>
<div class="report-meta">${meta}</div>
</header>
<main>${contentHtml}</main>
<footer class="report-footer">导出时间 ${ts} · AI 生成内容仅供参考, 不构成投资建议</footer>
</div>
</body>
</html>`
}

function safeFilename(input: ReportExportInput): string {
	return (input.filename ?? input.title).replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 120)
}

function triggerDownload(blob: Blob, filename: string): void {
	const url = URL.createObjectURL(blob)
	const a = document.createElement('a')
	a.href = url
	a.download = filename
	a.click()
	setTimeout(() => URL.revokeObjectURL(url), 30_000)
}

/** 导出为自包含 HTML 文件下载。 */
export function downloadReportHtml(input: ReportExportInput): void {
	const blob = new Blob([buildReportHtml(input)], { type: 'text/html;charset=utf-8' })
	triggerDownload(blob, `${safeFilename(input)}.html`)
}

/** 导出原始 Markdown 源文件 (.md)。 */
export function downloadReportMarkdown(input: ReportExportInput): void {
	const blob = new Blob([input.markdown], { type: 'text/markdown;charset=utf-8' })
	triggerDownload(blob, `${safeFilename(input)}.md`)
}

/** 隐藏 iframe 加载导出 HTML, 返回 iframe (调用方负责 remove)。 */
function loadHiddenIframe(html: string): Promise<HTMLIFrameElement> {
	return new Promise((resolve, reject) => {
		const iframe = document.createElement('iframe')
		iframe.style.cssText = 'position:fixed;right:0;bottom:0;width:920px;height:600px;visibility:hidden;border:0;'
		iframe.onload = () => resolve(iframe)
		iframe.onerror = () => reject(new Error('导出框架加载失败'))
		document.body.appendChild(iframe)
		iframe.srcdoc = html
	})
}

/** 导出 PDF: 打开浏览器原生打印对话框, 目标选 "另存为 PDF"。 */
export async function printReportPdf(input: ReportExportInput): Promise<void> {
	const iframe = await loadHiddenIframe(buildReportHtml(input))
	try {
		const win = iframe.contentWindow
		if (!win) throw new Error('导出框架不可用')
		win.focus()
		win.print()
	} finally {
		// 打印对话框关闭后再移除 (延迟兜底, 部分浏览器 print 立即返回)
		setTimeout(() => iframe.remove(), 60_000)
	}
}

/** 导出 PNG 图片 (SVG foreignObject → canvas, 2x 缩放; 超长报告降为 1x 防 canvas 超限)。 */
export async function exportReportPng(input: ReportExportInput): Promise<void> {
	const iframe = await loadHiddenIframe(buildReportHtml(input))
	try {
		const doc = iframe.contentDocument
		if (!doc?.documentElement) throw new Error('导出框架不可用')
		const width = 920
		const height = Math.min(Math.ceil(doc.documentElement.scrollHeight) + 8, 20_000)
		const xhtml = new XMLSerializer().serializeToString(doc.documentElement)
		const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">`
			+ `<foreignObject width="100%" height="100%">${xhtml}</foreignObject></svg>`
		const img = new Image()
		img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
		await img.decode()
		const scale = height * 2 > 16_000 ? 1 : 2
		const canvas = document.createElement('canvas')
		canvas.width = width * scale
		canvas.height = height * scale
		const ctx = canvas.getContext('2d')
		if (!ctx) throw new Error('canvas 不可用')
		ctx.fillStyle = '#fff'
		ctx.fillRect(0, 0, canvas.width, canvas.height)
		ctx.scale(scale, scale)
		ctx.drawImage(img, 0, 0, width, height)
		const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/png'))
		if (!blob) throw new Error('图片编码失败')
		triggerDownload(blob, `${safeFilename(input)}.png`)
	} finally {
		iframe.remove()
	}
}
