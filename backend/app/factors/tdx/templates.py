"""通达信公式模板库 (P1-17 第 3 批)。

给「照着改」的起点, 不是给「照着抄」的成品: 每条都是能直接编译通过的完整公式,
用户改窗口参数或换比较条件就能得到自己的因子。

约定:
- 全部用 `:=` 中间变量写, 与通达信习惯一致, 同时兼作第 3 批多语句能力的示例;
- 一个因子只能有**一个**数值, 所以 BOLL 这类多线指标必须收敛成单一量
  (这里取「价格在带内的相对位置」0~1), 三条线本身不能同时输出;
- `kind` 区分产出: value = 连续数值(可参与打分), signal = 布尔(可直接当选股条件);
- 模板与 tests/test_factor_tdx.py 里的编译断言一一对应, 改公式必须同步改测试。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class TdxTemplate:
    """一条可直接使用的通达信公式模板。"""

    id: str
    name: str
    category: str  # 指标族: 趋势 / 摆动 / 通道 / 均线
    kind: str  # value = 连续数值; signal = 布尔选股条件
    formula: str
    description: str


TDX_TEMPLATES: tuple[TdxTemplate, ...] = (
    TdxTemplate(
        id="macd_hist",
        name="MACD 柱",
        category="趋势",
        kind="value",
        formula=(
            "DIF:=EMA(CLOSE,12)-EMA(CLOSE,26);\n"
            "DEA:=EMA(DIF,9);\n"
            "MACD柱:(DIF-DEA)*2;"
        ),
        description="快慢指数均线之差再平滑, 柱体由负转正通常意味着动能转强。",
    ),
    TdxTemplate(
        id="macd_golden_cross",
        name="MACD 金叉",
        category="趋势",
        kind="signal",
        formula=(
            "DIF:=EMA(CLOSE,12)-EMA(CLOSE,26);\n"
            "DEA:=EMA(DIF,9);\n"
            "CROSS(DIF,DEA)"
        ),
        description="DIF 上穿 DEA 的那一根为真, 可直接当选股条件用。",
    ),
    TdxTemplate(
        id="kdj_j",
        name="KDJ 的 J 值",
        category="摆动",
        kind="value",
        formula=(
            "RSV:=(CLOSE-LLV(LOW,9))/(HHV(HIGH,9)-LLV(LOW,9))*100;\n"
            "K:=SMA(RSV,3,1);\n"
            "D:=SMA(K,3,1);\n"
            "J:3*K-2*D;"
        ),
        description="J 值放大 K/D 的背离, 高于 100 或低于 0 常被视为超买超卖。",
    ),
    TdxTemplate(
        id="rsi_6",
        name="RSI(6)",
        category="摆动",
        kind="value",
        formula=(
            "LC:=REF(CLOSE,1);\n"
            "RSI6:SMA(MAX(CLOSE-LC,0),6,1)/SMA(ABS(CLOSE-LC),6,1)*100;"
        ),
        description="6 日相对强弱, 0~100; 横盘无波动时分母为 0, 结果为空值而非报错。",
    ),
    TdxTemplate(
        id="boll_position",
        name="BOLL 带内位置",
        category="通道",
        kind="value",
        formula=(
            "MID:=MA(CLOSE,20);\n"
            "UPPER:=MID+2*STD(CLOSE,20);\n"
            "LOWER:=MID-2*STD(CLOSE,20);\n"
            "POS:(CLOSE-LOWER)/(UPPER-LOWER);"
        ),
        description="收盘价在布林带内的相对位置, 0=下轨 0.5=中轨 1=上轨, 可超出区间。",
    ),
    TdxTemplate(
        id="ma_bullish_alignment",
        name="均线多头排列",
        category="均线",
        kind="signal",
        formula="MA(C,5)>MA(C,10) AND MA(C,10)>MA(C,20)",
        description="短中长均线依次向上排列, 单表达式写法, 不需要中间变量。",
    ),
)


def tdx_templates() -> list[dict]:
    """导出模板列表 (前端「公式模板」选择器用, 与 TDX_TEMPLATES 同源不脱节)。"""
    return [asdict(t) for t in TDX_TEMPLATES]
