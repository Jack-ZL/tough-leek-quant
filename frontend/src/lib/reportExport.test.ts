import { describe, expect, it } from 'vitest'
import { buildReportHtml } from './reportExport'

describe('buildReportHtml (P1-21 报告导出)', () => {
	it('标题转义 + meta 空项过滤 + 免责声明', () => {
		const html = buildReportHtml({
			title: '测试 <script> 报告',
			meta: ['600000 浦发银行', ''],
			markdown: '# 标题一\n\n正文内容',
		})
		expect(html).toContain('测试 &lt;script&gt; 报告')
		expect(html).not.toContain('<script>')
		// 空 meta 项被过滤: report-meta 内只剩一个 div
		expect(html).toContain('<div class="report-meta"><div>600000 浦发银行</div></div>')
		expect(html).toContain('AI 生成内容仅供参考')
		expect(html).toContain('<style>')
		expect(html).toContain('lang="zh-CN"')
	})

	it('Markdown 经 MarkdownRenderer 渲染为 HTML (标题/表格/加粗)', () => {
		const html = buildReportHtml({
			title: 'T',
			markdown: '## 二级标题\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n**加粗**',
		})
		// MarkdownRenderer 的标题输出为带 font-semibold 的 div (非 h 标签)
		expect(html).toContain('font-semibold')
		expect(html).toContain('二级标题')
		expect(html).toContain('<table')
		expect(html).toContain('<strong class="font-semibold text-foreground">加粗</strong>')
	})
})
