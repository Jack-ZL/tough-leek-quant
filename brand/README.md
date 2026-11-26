# TLQ 品牌资产 · 唯一来源

主标采用**双引精修版**：两笔共用水平品牌红渐变，中央留白勾出一根 bullish K 线。方形主标使用 64 × 64 网格，全直角（无圆角/圆笔帽）。当前资产的预览与下载见 [`logo-assets.html`](./logo-assets.html)；19 个方向的设计提案与评审记录（历史几何）见 [`logo-proposals.html`](./logo-proposals.html)。

## SVG 源文件（矢量，优先使用）

| 文件 | 变体 | 场景 |
| --- | --- | --- |
| [`logo.svg`](./logo.svg) | 渐变 · 浅底 `#F47971 → #F04438 → #D51D10` | 默认：README / 文档 / 浅色 UI |
| [`logo-dark.svg`](./logo-dark.svg) | 渐变 · 深底（亮红）`#F79D97 → #F47971 → #F04438` | 终端 / 暗色 UI |
| [`logo-white.svg`](./logo-white.svg) | 反白 · 纯白 `#FFFFFF` | 页头 / 启动页 / 品牌色背景 |
| [`logo-badge.svg`](./logo-badge.svg) | 徽章 · 方底白标 `#F04438 → #B4190E` | App 图标 / 头像（平台圆角由宿主裁切） |
| [`logo-tile-light.svg`](./logo-tile-light.svg) | 场景底卡 · 浅色圆角卡 `#F6F6F8` 底 | 文档配图 / 社交卡片（浅色） |
| [`logo-tile-dark.svg`](./logo-tile-dark.svg) | 场景底卡 · 深色圆角卡 `#191C25` 底 + `#232838` 描边 | 文档配图 / 社交卡片（深色） |
| [`logo-title.svg`](./logo-title.svg) / [`logo-dark-title.svg`](./logo-dark-title.svg) | 裁剪版 · viewBox 裁至墨迹底边 (`0 0 64 55.5`) | **README 标题行内嵌** — 墨迹贴基线, 与文字垂直居中 |

渐变均为水平三段（`x1=8.5 y1=32 → x2=55.5 y2=32`，`userSpaceOnUse`），横跨两笔墨迹，因此左右笔共享同一条色带。

## PNG 与桌面图标

- `png/logo-{16,24,32,48,64,128,256,512}.png` — 浅底渐变（标准版）
- `png/logo-dark-{16,24,32,48,64,128,256,512}.png` — 深底亮红
- `png/logo-white-{16,24,32,48,64,128,256,512}.png` — 纯白
- `png/logo-badge-{32,48,64,128,256,512,1024}.png` — 头像徽章
- `png/logo-tile-{light,dark}-{128,512}.png` — 场景底卡（圆角 12.5%，主标占比 75% 居中）
- `png/installer-wizard.png` — Windows 安装向导配图；欢迎页与完成页的图案由深底亮红主标生成
- `png/menu-icons-preview.png` — 侧栏菜单图标族对照表（19 个, 源码 `frontend/src/components/BrandIcons.tsx`）
- `icon.ico` — Windows 应用图标（内嵌 16 / 24 / 32 / 48 / 64 / 128 / 256 七档，均为 32bpp PNG；浅色圆角卡版，由 `logo-tile-light.svg` 派生 —— 透明底渐变 logo 在桌面快捷方式上显得过空，圆角浅卡在深/浅壁纸均成立）
- `icon.icns` — macOS 应用图标（16–1024 八档，含 @2x；与 `icon.ico` 同源同版）
- 打包配置直接引用本目录的图标（`packaging/tsp.spec`、`packaging/tsp.iss`、`.github/workflows/release.yml`）。macOS DMG 挂载后的卷图标与卷内应用都使用 `icon.icns`。

## 项目接入

- **共用组件**：侧栏、登录、初始化和路由加载页统一使用 `frontend/src/components/Logo.tsx`；界面图标继承所在页面的配色。
- **favicon**：`frontend/public/favicon.svg`（几何与本套一致，按系统明暗模式选择渐变）。
- **README / 文档**：首屏用 `picture` 切换浅色 `logo.svg` 与深色 `logo-dark.svg`；徽章位可用 `png/logo-128.png`。
- **GitHub 社交头像**：`png/logo-badge-1024.png`。
- **小图标**：使用已有的 16、24 或 32 像素 PNG 预缩版本，等比缩放，保留中间的 K 线留白。

## 使用规范

- 网格 64×64；两笔均为**填充路径**（无描边、无圆角、无圆笔帽）；渐变水平 `x 8.5 → 55.5`，三段色标 `0 / .5 / 1`
- 徽章版：方底 60×60（`x=2 y=2`），标记纯白，按 64 网格绘制后 `translate(8 8) scale(.75)` 居中
- 底卡版：圆角 12.5%（`rx=8`），标记同样 `translate(8 8) scale(.75)` 居中；深色卡描边 `#232838` @0.6
- 裁剪版：viewBox 收到 `0 0 64 55.5`，仅去掉下沿留白，不得平移或缩放墨迹
- **中央 K 线留白不得填充、描边或加投影** —— 它是主标的识别特征
- 最小可用尺寸 **16px**；小于 32px 优先用 PNG 预缩版本而非浏览器缩放
- 保持全直角语言：不得添加圆角、描边、投影或渐变以外的色彩
- 周边留白：图形外保留 ≥ 1/8 图形宽度的净空

## 更新后自检

更新品牌资源后，分别检查 README 浅深色、浏览器标签页及桌面安装包：macOS 查看安装后应用与 Dock，Windows 查看安装向导及应用图标。
