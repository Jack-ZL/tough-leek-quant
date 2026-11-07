import { useDialogTask, useDialogState } from '@/lib/stockAnalysisStore'
import { StockAnalysisDialog } from './StockAnalysisDialog'
import { DebateDialog } from './DebateDialog'

/**
 * AI 个股分析对话框宿主 —— 单点挂载在 Layout。
 * 与财务分析的 AiAnalysisHost 并列,独立 store,蓝色主题。
 * 多空辩论对话框 (P1-9) 也挂在这里 — 由个股分析对话框的 ⚔️ 按钮唤起。
 */
export function StockAnalysisHost() {
	const { task, mode } = useDialogTask()
	const { minimized } = useDialogState()
	return (
		<>
			<StockAnalysisDialog task={task} mode={mode} minimized={minimized} />
			<DebateDialog />
		</>
	)
}
