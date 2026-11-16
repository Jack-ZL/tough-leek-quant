// 板块类页面 (概念板块 / 行业板块) 的各市场数据源就绪度与空态文案。
//
// 为什么单独一个模块: 两个板块页是**同构的自定义扩展页** (`custom/concept-analysis`、
// `custom/industry-analysis`), 无源结论也来自同一次实测 (方案 §3.4)。文案若各写一份,
// 将来找到港股板块源时必然只改一处 —— 另一处继续谎称「无源」。
//
// 🔴 文案**不进** `lib/marketContent.ts`: 那张表只放市场差异**事实** (label/货币/时区/
// 核心指数/代码形态), 「为什么没有数据」是数据源侧知识 (marketContent.ts 头注同口径,
// 与两个适配器的 `emptyHint`/`dailyEmptyHint` 分工一致)。
//
// 🔴 用 `Record<Market, …>` 而不是 `market === 'HK'` 字面量分支: 新增市场时这里会
// **编译报错**, 逼作者显式回答「这个市场有没有板块源」, 而不是静默落进 else 把 A 股
// 板块当港股渲染 (方案 §8 规则 2; 同构范式见 `components/StockDetailBody.tsx` 的 BODIES)。
import type { Market } from './market'

export type BoardKind = 'concept' | 'industry'

export interface BoardNoSourceCopy {
	title: string
	hint: string
}

/**
 * `null` = 该市场有源, 页面正常渲染; 非 null = 无源, 整页只渲染这段空态文案
 * (CONTRIBUTING §4 fail closed: 宁可明说没有, 不可用 A 股数据冒充)。
 */
export const BOARD_NO_SOURCE_COPY: Record<BoardKind, Record<Market, BoardNoSourceCopy | null>> = {
	concept: {
		CN: null,
		HK: {
			title: '港股无概念板块数据源',
			hint: HK_NO_SOURCE_HINT('概念'),
		},
	},
	industry: {
		CN: null,
		HK: {
			title: '港股无行业板块数据源',
			hint: HK_NO_SOURCE_HINT('行业'),
		},
	},
}

/** 取某板块页在某市场的空态文案; 有源时返回 null。 */
export function boardNoSourceCopy(kind: BoardKind, market: Market): BoardNoSourceCopy | null {
	return BOARD_NO_SOURCE_COPY[kind][market]
}

// 实测结论 (方案 §3.4): 东财板块中心 `push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:{1,2,3}`
// 返回 31 地域 / 496 行业 / 554 概念, **全是 A 股** (BK0882 猪肉概念 等); `t:4/t:5/t:6`
// 返回空。akshare `stock_board.html` 里港股只有**人气榜**, 板块接口全是
// `stock_board_concept_*_em` / `stock_board_industry_*_em` / `*_ths` (A 股)。
// 东财网页版同样没有港股板块。
function HK_NO_SOURCE_HINT(kindLabel: string): string {
	return `已实测确认：东财板块中心的地域/行业/概念三类板块全部只覆盖 A 股，akshare 港股侧仅提供人气榜，`
		+ `东财网页版同样没有港股板块分类。因此本页不渲染任何港股${kindLabel}数据，也不会用 A 股${kindLabel}冒充`
		+ `（CONTRIBUTING §4 fail closed：宁可明说没有，不可显示假数据）。`
		+ `${kindLabel}成分走扩展数据通道，将来若找到港股${kindLabel}源，在「数据 → 扩展数据」配一张表即可接入，无需改代码。`
		+ `查看 A 股${kindLabel}板块请用侧栏市场切换器切回 A 股。`
}
