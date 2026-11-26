<<<<<<< ours (dev)
// 原创 logo:方括号 [ ] 包裹一根带 wick 的 K 线
//
// 概念:
//   - 外层 brackets:终端 / 代码 / 引用边界 — 赛博 + quant 气质
//   - 中央 wick+body:一根标准 K 线 — 直接的金融指代
//   - body 偏上 + 下影长:bullish 站稳感 (上影短 / 下影长)
//
// 用 currentColor,继承父级 color 设定,方便切换品牌色。

// 品牌色 — 只用于 logo / brand 区域,不影响功能语义色 (= CSS --bull)
export const BRAND = '#F04438'

||||||| normalised base
// 原创 logo:方括号 [ ] 包裹一根带 wick 的 K 线
//
// 概念:
//   - 外层 brackets:终端 / 代码 / 引用边界 — 赛博 + quant 气质
//   - 中央 wick+body:一根标准 K 线 — 直接的金融指代
//   - body 偏上 + 下影长:bullish 站稳感 (上影短 / 下影长)
//
// 用 currentColor,继承父级 color 设定,方便切换品牌色。
=======
// 双引标的中央留白形成 K 线。界面沿用调用方的颜色和尺寸。
>>>>>>> theirs (main)
interface LogoProps {
	className?: string
	size?: number
	style?: React.CSSProperties
}

export function Logo({ className, size = 32, style }: LogoProps) {
	return (
		<svg
			viewBox="0 0 64 64"
			width={size}
			height={size}
			fill="currentColor"
			className={className}
			style={style}
			role="img"
			aria-label="Tough Leek Quant"
		>
			<path
				d="M8.5 32.5C8.5 19.25 15.75 10.5 27.5 10.5H35.875L29.875 16.5V21.5H26.125V40H29.875V46.5L20.875
					 55.5H8.5Z"
			/>
			<path
				d="M34.125 17.5L43.125 8.5H55.5V31.5C55.5 44.75 48.25 53.5 36.5 53.5H28.125L34.125
					 47.5V40H37.875V21.5H34.125Z"
			/>
		</svg>
	)
}
