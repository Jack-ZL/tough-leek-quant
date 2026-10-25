"""AI 输出数字忠实度闸门 (L3, 设计文档: docs/ai-number-fidelity-plan.md)。

原理一句话: AI 只能"抄"或"算", 不能"编"。
1. 生成前把发给模型的数据/提示词里的数字收进白名单池 (NumberPool, 见
   ai_number_whitelist.py);
2. 生成后用正则把回答里每个数字 token 抽出来, 规范化 (全角→半角、剥零宽、
   繁体单位→简体、万/亿/% 换算), 到池里带容差二分查找;
3. 找不到出处、也不是回答内算式算出来的数字 → 就地包进「〖…※〗」高亮标记——
   数字保留可见 (用户要求突出显示而非隐藏), 绝不拒绝整段回答 (对齐
   factors/dsl.py "错误即数据" 先例);
4. 流式链路按行缓冲: 数字永远不会未经检查就 emit 给前端。

参照 Vibe-Research orchestrator/src/number_gate.ts (对话侧"整轮资料池"模型)
的 Python 复刻 + 增强: Vibe 的 JS \\d 不匹配全角数字、未做规范化, 全角
"１２３"、零宽字符插数字、繁体"億"在 Vibe 对话闸是漏检的; 本模块先做
字符级规范化再提取, 补上这个缺口。

fail-closed 哲学 (对齐 CONTRIBUTING §8 与 enriched_generation 先例):
- 白名单为空/采集失败 → 打码全部非豁免数字, 绝不放行;
- 闸门自身抛异常 → 该段全部打码 + error 日志, 流不中断 (打码是安全侧);
- violations 详情只进服务端日志, 不透给客户端。

已知边界 (设计文档 §14):
- 只查"数字是不是凭空编的", 不查"张冠李戴" (真数字安错主体);
- 日期/时刻/序号/股票代码/指标参数/指数名/小整数计数在豁免清单里不核对,
  因此"编日期"抓不到 (需要单独的交易日历核对, 不在本期范围);
- 消息面/新闻类章节数字来自联网搜索或模型记忆, 不在本地白名单 → 该类
  章节放宽为"只记录不打码" (对齐 regime_alignment "边界宽内部严" 先例)。
"""
from __future__ import annotations

import ast
import json
import logging
import re
from bisect import bisect_left
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 高亮标记: 无出处的数字【原样保留、外包 〖…※〗】— 用户要求问题数字可见
# 且突出显示, 不做隐藏式替换。〖〗 不含 |、不命中 MarkdownRenderer 任何
# 块级/行内正则 → 逐字安全渲染; 展示层 (web/钉钉/企微) 各自换成平台高亮样式。
MARK_OPEN = "〖"
MARK_CLOSE = "〗"
# 匹配完整标记段; group 1 = 被标出的原文 (数字+单位)
MARKED_RE = re.compile(r"〖([^〗]*)※〗")


def wrap_marked(span: str) -> str:
    """把无出处数字段包成 〖原文※〗 高亮标记形式。"""
    return f"{MARK_OPEN}{span}※{MARK_CLOSE}"


def emphasize_marked_dingtalk(text: str) -> str:
    """〖X※〗 → **⚠️X** (钉钉 markdown 支持加粗+emoji, 不支持字体色)。"""
    return MARKED_RE.sub(r"**⚠️\1**", text)


def emphasize_marked_wecom(text: str) -> str:
    """〖X※〗 → warning 字体色 (企微 markdown 支持 <font color>)。"""
    return MARKED_RE.sub(r'<font color="warning">⚠️\1</font>', text)

# 相对容差 0.2%: 与 Vibe number_gate 一致, 覆盖 float 噪声与四舍五入尾差
_REL_TOL = 2e-3

# 量级单位 → 倍数 (长词优先匹配, "万亿元"必须先于"万")
MULT: dict[str, float] = {"万亿": 1e12, "千万": 1e7, "百万": 1e6, "亿": 1e8, "万": 1e4}

# 百分比类单位 (值语义 = 数字/100)
_PCT_UNITS = frozenset({"%", "百分点", "pp"})

# 单位别名 → 规范单位: 量级单位保留名称 (进 plain 池配对), 货币/倍数类归 ""
_UNIT_CANON = {
    "万亿元": "万亿", "亿元": "亿", "万元": "万", "个亿": "亿", "个万": "万",
    "元/股": "", "美元": "", "港元": "", "元": "", "倍": "", "x": "", "X": "",
    "百分点": "%", "pp": "%",
}

# 数字后紧跟的单位 (标记时连单位一起包进 〖…〗, 避免留下"〖88.8〗亿元"; 长词优先)
UNIT_AFTER = re.compile(
    r"\s{0,2}(?:万亿元|亿元|万元|个亿|个万|万亿|千万|百万|亿|万|元/股|美元|港元|元"
    r"|百分点|pp|%|‰|倍|x(?![A-Za-z0-9]))",
    re.I,
)

# 小整数豁免的判定单位: 后面跟了这些单位的整数是数据主张, 不豁免
# (计划文档 §5.1: "3 个理由"豁免, "5%"/"11 期"/"3 倍"不豁免)
_UNIT_OR_COUNT = re.compile(
    r"\s{0,2}(?:%|‰|百分点|pp|倍|元|美元|港元|个亿|个万|万亿|千万|百万|亿|万|期"
    r"|x(?![A-Za-z0-9]))",
    re.I,
)

# 主提取正则: 千分位只有"逗号后正好 3 位"才算 (防"1,33 家"被拼成 133)
NUM_RE = re.compile(
    r"-?\d{1,3}(?:,\d{3})+(?![\d,])(?:\.\d+)?(?:e[+-]?\d+)?"
    r"|-?\d+\.?\d*(?:e[+-]?\d+)?",
    re.I,
)

# ----------------------------------------------------------------
# 规范化 (补 Vibe 缺口: 全角数字/零宽字符/繁体单位)
# ----------------------------------------------------------------

_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff")
_TRAD_MAP = {"億": "亿", "萬": "万"}
# 各类数学/排版横线统一成 ASCII "-": 模型爱用 U+2212 减号写算式
_DASH_CHARS = frozenset("−–—‐‑⁃")


def _normalize(text: str) -> tuple[str, list[int]]:
    """字符级规范化, 返回 (norm, idx_map); idx_map[i] = norm 第 i 字符的原文下标。

    刻意不做整段 NFKC: NFKC 会把中文全角标点 (，。！) 转成半角, 破坏整篇
    报告的排版; 这里只转换影响数字提取的字符:
    - 全角 ASCII (U+FF01..FF5E) → 半角 (１→1, ％→%, ．→., ＋→+);
    - 零宽字符 → 删除 (防"1\\u200b56.3"拆开逃避核对, 必须读成 156.3);
    - 繁体单位 億/萬 → 亿/万; 数学横线 −–— → -; 全角空格 → 半角空格。
    idx_map 用于把打码位置映射回原文, 输出文本除打码段外逐字保留原样。
    """
    chars: list[str] = []
    idx_map: list[int] = []
    for i, ch in enumerate(text):
        if ch in _ZERO_WIDTH:
            continue
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            ch = chr(cp - 0xFEE0)
        elif cp == 0x3000:
            ch = " "
        elif ch in _TRAD_MAP:
            ch = _TRAD_MAP[ch]
        elif ch in _DASH_CHARS:
            ch = "-"
        chars.append(ch)
        idx_map.append(i)
    return "".join(chars), idx_map


# ----------------------------------------------------------------
# 豁免清单 (等长空格遮蔽, 保持 index 对齐; 参照 number_gate.ts PRE_MASK)
# ----------------------------------------------------------------

# 闭合代码围栏: 只有"半数以上行像代码"才豁免 (防把编的数字塞进 ``` 绕过);
# json/yaml/csv 围栏是数据不是代码, 永远核对 (计划文档用例 5)
_FENCE_RE = re.compile(r"(?m)^```([A-Za-z0-9+#_-]*)[ \t]*\r?\n(.*?)^```[ \t]*$", re.S)
_FENCE_ALWAYS_CHECKED = {"json", "jsonc", "yaml", "yml", "csv", "tsv"}
_CODE_LINE_RE = re.compile(
    r"^\s*(?:import\s|from\s|def\s|class\s|return\b|if\s|for\s|while\s|const\s"
    r"|let\s|var\s|function\s|//|#|\}|\{|<|[@\w$.]+\s*[=(])"
)

# 日期/时刻/序号/指标参数/时间窗口等"非数据主张"数字
_EXEMPT_PATTERNS = (
    # 完整日期: 2024-10-08 / 2024/10/08 / 2024年10月8日
    # 年份限定 19xx/20xx + 月/日限定合法范围 + 前面不能是数字/点 — 否则
    # "1234.56.78" 这类多位小数会被误当日期豁免 (漏核对方向错误, 宁严勿漏)
    re.compile(r"(?<![\d.])(?:19|20)\d{2}\s*[-/.年]\s*(?:0?[1-9]|1[0-2])"
               r"\s*[-/.月]\s*(?:0?[1-9]|[12]\d|3[01])\s*日?"),
    # 月日: 10月28日 (后面不能跟数字/点 — "3月4.5%" 的 4.5 是数据不是日期)
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日?(?![\d.])"),
    # 年月: 2024-10 / 2024.10。同款年份+月份范围限定; 后跟数据单位
    # (元/万/亿/%) 时不豁免 — "2024.5 万元" 是金额不是日期
    re.compile(r"(?<![\d.])(?:19|20)\d{2}\s*[-/.]\s*(?:0?[1-9]|1[0-2])"
               r"(?![\d.]|\s*[元万亿%‰倍股])"),
    re.compile(r"(?<![\d.])(?:19|20)\d{2}\s*年(?!\d)"),
    # 合法时刻 09:35 / 14:30:00 (时 0-23 分 0-59; "配比 35:65" 不合法 → 仍核对)
    re.compile(r"(?<![\d:])(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?![\d:])"),
    # 短日期 10-08 (前后不能是数字/点/横线, 避免误伤小数区间)
    re.compile(r"(?<![\d.-])(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?![\d.-])"),
    # 行首列表序号: "1. " / "3、" / "2)" (后面必须跟空白或行尾, 不误伤 "1.5元")
    re.compile(r"(?m)^\s{0,3}\d{1,2}\s*[.、)）](?=\s|$)"),
    # 第 N 章/节/季 (期 不豁免: "11 期" 是数据主张)
    re.compile(r"第\s*\d{1,3}\s*[章节篇部季]"),
    # 指标参数组: MACD(12,26,9) / RSI(6)
    re.compile(r"[A-Za-z]{2,6}\s*\(\s*\d{1,4}(?:\s*[,，]\s*\d{1,4})*\s*\)"),
    # 均线名: MA5 / EMA10 / 60日均线 / 5日线
    re.compile(r"(?:MA|EMA|SMA|WMA|DMA)\s*\d{1,3}", re.I),
    re.compile(r"\d{1,3}\s*[日周月年]\s*均?\s*线"),
    # 时间窗口: 近 5 日 / 过去 3 个月 / 未来 2 周 / 10 个交易日
    re.compile(r"(?:近|最近|过去|未来|前后|连续|保持)\s*\d{1,4}\s*(?:个)?\s*"
               r"(?:交易日|日|天|周|月|年|小时|分钟)"),
    re.compile(r"\d{1,4}\s*个交易日"),
    # 闸门自己的页脚说明 (幂等: 打码结果再过一遍闸门不重复打码)
    re.compile(r"※\s*另有\s*\d+\s*处"),
    # 已标出的 〖…〗 段整体豁免 (幂等: 标记内的数字不再重复核对/嵌套包裹)
    re.compile(r"〖[^〗]*〗"),
)

# 股票代码: 6 位数字, 可带 SH/SZ/BJ 前缀或 .SH 后缀。
# 后面紧跟单位 (元/%/万…) 的不豁免——那是金额不是代码 ("123456元")。
_CODE_PAT = re.compile(
    r"(?<![A-Za-z\d.])(?:[A-Za-z]{2}\.?)?\d{6}(?:\.[A-Za-z]{2})?(?!\d)(?!\.\d)"
)

# 含数字的指数/指标名词表 (未注册的名词其数字照常核对——只会多打码不会
# 漏核对, fail-safe 方向正确; 计划文档用例 8)
_DEFAULT_LABELS = (
    "沪深300", "中证500", "中证1000", "中证2000", "中证A50", "中证A500",
    "上证50", "上证180", "上证380", "深证100", "创业板50", "科创50", "科创100",
    "北证50", "国证2000", "恒生指数", "标普500", "纳斯达克100", "道琼斯",
)
_labels: list[str] = list(_DEFAULT_LABELS)
_labels_re: re.Pattern[str] | None = None


def register_gate_labels(*labels: str) -> None:
    """注册含数字的指数/指标名词 (如自定义 "XX500"), 豁免其中的数字部分。"""
    global _labels_re
    for lab in labels:
        lab = lab.strip()
        if lab and lab not in _labels:
            _labels.append(lab)
    _labels_re = None


def reset_gate_labels() -> None:
    """恢复内置词表 (测试隔离用)。"""
    global _labels_re
    _labels[:] = list(_DEFAULT_LABELS)
    _labels_re = None


def _labels_regex() -> re.Pattern[str]:
    global _labels_re
    if _labels_re is None:
        ordered = sorted(_labels, key=len, reverse=True)
        _labels_re = re.compile("|".join(re.escape(x) for x in ordered))
    return _labels_re


def _mask_exemptions(norm: str, *, small_int: bool = True) -> str:
    """把豁免 span 替换成等长空格 (保持 index 对齐, 提取/打码共用此坐标系)。

    small_int=False 时跳过"小整数计数"豁免——算式解析需要看到 "÷ 8" 里的 8
    (运算数是否豁免在算式核对时单独判断)。
    """
    out = list(norm)

    def blank(start: int, end: int) -> None:
        for i in range(start, min(end, len(out))):
            if out[i] != "\n":
                out[i] = " "

    # 1) 闭合代码围栏 (半数以上行像代码才豁免)
    for m in _FENCE_RE.finditer(norm):
        lang = m.group(1).lower()
        if lang in _FENCE_ALWAYS_CHECKED:
            continue
        lines = [ln for ln in m.group(2).splitlines() if ln.strip()]
        if not lines:
            continue
        codeish = sum(1 for ln in lines if _CODE_LINE_RE.match(ln))
        if codeish * 2 > len(lines):
            blank(m.start(), m.end())

    # 2) 日期/时刻/序号/指标参数/时间窗口
    for pat in _EXEMPT_PATTERNS:
        for m in pat.finditer(norm):
            blank(m.start(), m.end())

    # 3) 股票代码 (后跟单位的按金额核对, 不豁免)
    for m in _CODE_PAT.finditer(norm):
        if _UNIT_OR_COUNT.match(norm, m.end()):
            continue
        blank(m.start(), m.end())

    # 4) 指数/指标名词表
    for m in _labels_regex().finditer(norm):
        blank(m.start(), m.end())

    mid = "".join(out)
    if not small_int:
        return mid

    # 5) 小整数计数: |n|<=20 的整数且后面没跟数据单位 → 豁免
    #    ("3 个理由"豁免; "5%"/"3 倍"/"11 期" 是数据主张, 不豁免)
    out2 = list(mid)
    for m in NUM_RE.finditer(mid):
        raw = m.group()
        if "." in raw or "," in raw or "e" in raw.lower():
            continue
        if abs(int(raw)) <= 20 and not _UNIT_OR_COUNT.match(mid, m.end()):
            for i in range(m.start(), m.end()):
                out2[i] = " "
    return "".join(out2)


# ----------------------------------------------------------------
# 核心类型 (frozen dataclass, 对齐 CONTRIBUTING §4.3 "上下文优先不可变")
# ----------------------------------------------------------------

@dataclass(frozen=True)
class NumberPool:
    """数字白名单池 (双池设计, 防数量级造假——金融场景最危险的错误)。

    all:   排序去重的全部可接受数值 (含单位换算/双口径形态)。
    plain: 排序去重的 (数值, 量级单位) 对——数据里"以某量级单位原样展示"
           的形态。回答写"2 亿元"时, 原样读只和 plain 池的 (2, "亿") 比,
           资料里的"2 万元"或裸数字 2 都不能替它作证 (Vibe number_gate
           同款规则); 换算读 2e8 走 all 池。
    empty_reason: 非 None 表示采集失败/为空 → 触发 fail-closed 全打码。
    """

    all: tuple[float, ...] = ()
    plain: tuple[tuple[float, str], ...] = ()
    empty_reason: str | None = None


@dataclass(frozen=True)
class Violation:
    """一处无出处数字。只进服务端日志, 不透给客户端 (CONTRIBUTING §8)。"""

    raw: str          # 原始写法, 如 "2 亿元" 的 "2"
    value: float      # 规范化后数值
    line: int         # 行号 (定位用)
    reason: str       # no_source | magnitude 类同 no_source | arith_wrong
                      # | operand_no_source | dim_mismatch | direction_conflict
                      # | no_whitelist (fail-closed)
    index: int = 0    # 规范化文本中的起点 (打码用, 内部字段)
    unit_end: int = 0  # 含紧跟单位的终点 (打码时连单位一起吃)
    keep_dash: bool = False  # 前导 "-" 是区间/减号, 打码时保留


@dataclass(frozen=True)
class GateResult:
    text: str                    # 打码后文本 (mask_enabled=False 时原文)
    masked: int                  # 实际打码处数
    violations: tuple[Violation, ...]  # 全部违规 (含宽松章节只记录不打码的)


@dataclass(frozen=True)
class _Token:
    """一个待核对的数字 token (规范化文本坐标系)。"""

    raw: str
    value: float          # 带符号数值; 前导 "-" 判定为减号/区间号时已取绝对值
    start: int
    end: int
    unit: str             # 规范单位: 量级名 (亿/万/…) | "%" | "" (其他单位)
    unit_end: int
    neg_is_operator: bool  # 前导 "-" 是减号/区间号而非负号


@dataclass(frozen=True)
class _ArithResult:
    """一条 "A op B = R" 算式的核对结果。"""

    res_start: int    # 结果数字在规范化文本中的起点
    ok: bool
    reason: str       # "" | arith_wrong | operand_no_source | dim_mismatch


# ----------------------------------------------------------------
# 提取
# ----------------------------------------------------------------

def _canon_unit(unit_raw: str) -> str:
    u = unit_raw.strip()
    return _UNIT_CANON.get(u, u)


def _extract(masked: str) -> list[_Token]:
    """在豁免遮蔽后的规范化文本上提取全部数字 token。"""
    toks: list[_Token] = []
    for m in NUM_RE.finditer(masked):
        raw = m.group()
        try:
            value = float(raw.replace(",", ""))
        except ValueError:  # pragma: no cover - 正则保证可转换, 防御性兜底
            continue
        um = UNIT_AFTER.match(masked, m.end())
        unit_raw = um.group() if um else ""
        unit_end = um.end() if um else m.end()
        neg_is_op = False
        if raw.startswith("-"):
            # 前一个非空白字符是数字或运算符 → "-" 是减号/区间号不是负号
            # ("2.5-3.75" 的 3.75 按 +3.75 核对; "100 - 50" 的 50 同理)
            prev = masked[: m.start()].rstrip(" \t")
            if prev and (prev[-1].isdigit() or prev[-1] in ".(+-×÷*/="):
                value = abs(value)
                neg_is_op = True
        toks.append(_Token(raw, value, m.start(), m.end(),
                           _canon_unit(unit_raw), unit_end, neg_is_op))
    return toks


def _half_step(raw: str) -> float:
    """写到的那一位的半格: 写 "33" 允许 ±0.5, 写 "15.63" 允许 ±0.005。"""
    body = raw.replace(",", "")
    if "e" in body.lower():
        return 0.5
    m = re.search(r"\.(\d+)$", body)
    decimals = len(m.group(1)) if m else 0
    return 0.5 * (10 ** -decimals)


# ----------------------------------------------------------------
# 方向词 (符号承载: "下降 5.2%" 匹配资料里的 -5.2; 方向写反就是错)
# ----------------------------------------------------------------

_DIR_POS = ("上升", "上涨", "增加", "增长", "回升", "流入", "上行", "走强", "涨", "升")
_DIR_NEG = ("下降", "下跌", "减少", "回落", "下滑", "缩水", "流出", "下行", "走弱", "跌", "降")
# 方向词与数字之间只允许出现这些连接字符, 防"跌停价15.63"被误判方向
_DIR_CONNECTORS = frozenset(" \t:：为是了至到约达,，、（）()")


def _direction_signs(masked: str, tok: _Token) -> tuple[int, ...]:
    """返回该 token 应尝试的符号序列。

    - 显式负号 → 只按负值核对;
    - 紧邻方向词 (中间只有连接字符) → 只按该方向核对;
    - 无方向词 → 正负都试 (数据 -5.2, 文字写 "5.2%" 也算有出处)。
    """
    if tok.raw.startswith("-") and not tok.neg_is_operator:
        return (1,)
    window = masked[max(0, tok.start - 10) : tok.start]
    best: tuple[int, int] | None = None  # (词尾位置, 极性)
    for words, polarity in ((_DIR_POS, 1), (_DIR_NEG, -1)):
        for w in words:
            idx = window.rfind(w)
            while idx >= 0:
                end = idx + len(w)
                if all(ch in _DIR_CONNECTORS for ch in window[end:]):
                    if best is None or end > best[0]:
                        best = (end, polarity)
                    break
                idx = window.rfind(w, 0, idx)
    if best is None:
        return (1, -1)
    return (best[1],)


# ----------------------------------------------------------------
# 白名单匹配 (bound): 数值等价 + 容差, 不是字符串精确匹配
# ----------------------------------------------------------------

def _readings(tok: _Token) -> list[tuple[str, float, float, str]]:
    """token 的全部合理读法: (池名, 数值, 半格步长, plain 单位)。

    刻意只按"写出来的单位"换算, 不穷举 6 种量级 (Vibe 实测: 大池下随手编
    的 156.3 若允许 ÷100 就能撞上某个价格 15630, 产生假放行)。
    """
    half = _half_step(tok.raw)
    out: list[tuple[str, float, float, str]] = []
    scale = MULT.get(tok.unit) if tok.unit else None
    if scale is not None:
        # 写了量级单位: 原样读只认 plain 池同单位形态, 换算读进 all 池
        out.append(("plain", tok.value, half, tok.unit))
        out.append(("all", tok.value * scale, half * scale, ""))
    else:
        out.append(("all", tok.value, half, ""))
        if tok.unit == "%":
            out.append(("all", tok.value / 100, half / 100, ""))
    return out


def _near(arr: tuple[float, ...], v: float, tol: float) -> bool:
    """排序数组带容差查找 (bisect 二分, Vibe 同款: 30 万数字池 <3s)。"""
    i = bisect_left(arr, v - tol)
    return i < len(arr) and arr[i] <= v + tol


def _near_plain(arr: tuple[tuple[float, str], ...], v: float, tol: float, unit: str) -> bool:
    i = bisect_left(arr, (v - tol, ""))
    while i < len(arr) and arr[i][0] <= v + tol:
        if arr[i][1] == unit:
            return True
        i += 1
    return False


def _near_derived(derived: set[float], v: float, tol: float) -> bool:
    return any(abs(v - d) <= tol for d in derived)


def _hit(readings: Iterable[tuple[str, float, float, str]], sign: int,
         pool: NumberPool, derived: set[float]) -> bool:
    for kind, v, half, unit in readings:
        tol = max(abs(v) * _REL_TOL, half) + 1e-12
        if kind == "all":
            if _near(pool.all, sign * v, tol) or _near_derived(derived, sign * v, tol):
                return True
        elif _near_plain(pool.plain, sign * v, tol, unit):
            return True
    return False


def _bound_reason(tok: _Token, masked: str, pool: NumberPool,
                  derived: set[float]) -> str | None:
    """返回 None = 有出处; 否则返回违规原因。"""
    signs = _direction_signs(masked, tok)
    readings = _readings(tok)
    for s in signs:
        if _hit(readings, s, pool, derived):
            return None
    if len(signs) == 1 and _hit(readings, -signs[0], pool, derived):
        # 只有反方向的证据 → 方向写反就是错, 不放行 (计划文档用例 14)
        return "direction_conflict"
    return "no_source"


def _in_derived(tok: _Token, derived: set[float]) -> bool:
    """token 是否是回答内算式算出来的派生值 (符号不敏感)。"""
    if not derived:
        return False
    return _hit(_readings(tok), 1, _EMPTY_POOL, derived) or \
        _hit(_readings(tok), -1, _EMPTY_POOL, derived)


_EMPTY_POOL = NumberPool()


# ----------------------------------------------------------------
# 算式重算 (Vibe arithmetic.ts 的四则子集 + 链式派生)
# ----------------------------------------------------------------

# 从 "=" 向左回收的算式字符集 (碰到中文/标签文本自然停住, "涨幅 =" 前缀被剥掉)
_ARITH_ALLOWED = frozenset("0123456789.,+-×*/÷()%‰ \t")
_EXPR_TOKEN_RE = re.compile(
    r"\s+|(?P<num>\d[\d,]*(?:\.\d+)?)(?P<pct>%|‰)?|(?P<op>[+\-*/×÷])|(?P<paren>[()])"
)
_OP_MAP = {"×": "*", "÷": "/", "-": "-", "+": "+", "*": "*", "/": "/"}
# 派生值集合上限: 防超长回答无界增长 (正常报告远达不到)
_DERIVED_CAP = 4096


@dataclass(frozen=True)
class _Operand:
    value_written: float   # 按写法的数值 (查白名单用)
    canon: float           # 规范化数值 (重算用: %→/100, 亿→*1e8)
    unit: str              # 规范单位
    start: int             # 在规范化文本中的绝对位置 (查豁免/打码用)
    end: int
    raw: str


def _safe_eval(expr_src: str) -> float | None:
    """AST 白名单校验后求值: 只允许 数字/四则/一元正负/括号, 其余节点拒绝。"""
    try:
        tree = ast.parse(expr_src, mode="eval")
    except SyntaxError:
        return None
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            return None
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            return None
    try:
        # AST 已白名单校验, 表达式只含数字与四则运算符, 无注入面。
        # 先 compile 再 eval: Python 3.14 起 eval() 不再接受 AST 对象。
        code = compile(tree, "<ai_number_gate>", "eval")
        return float(eval(code, {"__builtins__": {}}, {}))
    except Exception:  # noqa: BLE001 - 除零/溢出等, 算式不成立按无派生处理
        return None


def _parse_expr(expr: str, offset: int, masked: str):
    """解析算式串 → (operands, ops, eval_src, dim_ok); 不合法返回 None。"""
    parts: list[tuple[str, Any]] = []
    pos = 0
    for m in _EXPR_TOKEN_RE.finditer(expr):
        if m.start() != pos:
            return None  # 出现算式字符集之外的东西 (豁免空格已含在 \s+? 否 → 拒绝)
        pos = m.end()
        if m.group().strip() == "" and not m.group("num") and not m.group("op") \
                and not m.group("paren"):
            continue  # 空白
        if m.group("num"):
            raw = m.group("num")
            try:
                v = float(raw.replace(",", ""))
            except ValueError:
                return None
            unit_raw = m.group("pct") or ""
            a_start = offset + m.start("num")
            a_end = offset + m.end("num")
            # 单位也可能紧跟在算式数字后 (如 "2亿+3亿"), 用 UNIT_AFTER 取规范单位
            um = UNIT_AFTER.match(masked, a_end)
            unit = _canon_unit(um.group()) if um else _canon_unit(unit_raw)
            if unit == "%":
                canon = v / 100
            elif unit in MULT:
                canon = v * MULT[unit]
            else:
                canon = v
            parts.append(("num", _Operand(v, canon, unit, a_start, a_end, raw)))
        elif m.group("op"):
            parts.append(("op", m.group("op")))
        else:
            parts.append(("paren", m.group("paren")))
    if pos != len(expr):
        return None

    operands = [p[1] for p in parts if p[0] == "num"]
    ops = [p[1] for p in parts if p[0] == "op"]
    if len(operands) < 2 or not ops:
        return None

    # 结构校验: 数字/括号与运算符交替, 括号平衡, 以数字或 ) 收尾
    depth = 0
    prev_kind = None
    for kind, val in parts:
        if kind == "paren":
            if val == "(":
                if prev_kind in ("num", "paren_close"):
                    return None
                depth += 1
            else:
                depth -= 1
                if depth < 0 or prev_kind not in ("num", "paren_close"):
                    return None
            prev_kind = "paren_open" if val == "(" else "paren_close"
        elif kind == "num":
            if prev_kind in ("num", "paren_close"):
                return None
            prev_kind = "num"
        else:
            if prev_kind not in ("num", "paren_close"):
                return None
            prev_kind = "op"
    if depth != 0 or prev_kind not in ("num", "paren_close"):
        return None

    # 量纲检查 (MVP): 加减两侧不能一边是百分比一边不是 ("2元 + 50%" 不认)
    dim_ok = True
    for i, (kind, val) in enumerate(parts):
        if kind == "op" and val in ("+", "-"):
            left = parts[i - 1] if i > 0 else None
            right = parts[i + 1] if i + 1 < len(parts) else None
            # 紧邻括号的是子表达式, MVP 不做子表达式量纲传播, 跳过该检查
            if left and left[0] == "num" and right and right[0] == "num":
                l_pct = left[1].unit == "%"
                r_pct = right[1].unit == "%"
                if l_pct != r_pct:
                    dim_ok = False
                    break

    eval_src = "".join(
        repr(p[1].canon) if p[0] == "num" else (_OP_MAP[p[1]] if p[0] == "op" else p[1])
        for p in parts
    )
    return operands, ops, eval_src, dim_ok


def _operand_sourced(op: _Operand, masked_small: str, pool: NumberPool,
                     derived: set[float]) -> bool:
    """运算数自己也要有出处: 白名单命中 / 派生值 / 本身在豁免区 (小整数等)。"""
    if masked_small[op.start : op.end].strip() == "":
        return True  # 该运算数落在豁免区 (如 "÷ 8" 的 8 是小整数豁免)
    tok = _Token(op.raw, op.value_written, op.start, op.end, op.unit, op.end, False)
    return _bound_reason(tok, masked_small, pool, derived) is None


def _check_arithmetic(masked: str, masked_small: str, pool: NumberPool,
                      derived: set[float]) -> list[_ArithResult]:
    """扫描 "A op B = R" 算式; 重算通过且运算数都有出处 → R 进 derived 集合。

    🔴 核心规则 (Vibe 同款): 运算数都有出处, 结果才有出处; 算错的、量纲
    混加的、运算数编造的, 结果一律不算派生值 → 主扫描按无出处打码。
    支持链式: 前面的算式结果可作为后面算式的运算数。
    """
    results: list[_ArithResult] = []
    for eq_m in re.finditer(r"=", masked):
        # 右侧: 结果数字 + 可选单位
        p = eq_m.end()
        while p < len(masked) and masked[p] in " \t":
            p += 1
        rm = NUM_RE.match(masked, p)
        if not rm:
            continue
        try:
            res_written = float(rm.group().replace(",", ""))
        except ValueError:
            continue
        um = UNIT_AFTER.match(masked, rm.end())
        res_unit = _canon_unit(um.group()) if um else ""
        if res_unit == "%":
            res_canon = res_written / 100
        elif res_unit in MULT:
            res_canon = res_written * MULT[res_unit]
        else:
            res_canon = res_written

        # 左侧: 从 "=" 向左回收算式字符 (遇中文/等号停住)
        s = eq_m.start()
        while s > 0 and masked[s - 1] in _ARITH_ALLOWED:
            s -= 1
        expr = masked[s : eq_m.start()]
        parsed = _parse_expr(expr, s, masked)
        if parsed is None:
            continue
        operands, _ops, eval_src, dim_ok = parsed
        if not dim_ok:
            results.append(_ArithResult(rm.start(), False, "dim_mismatch"))
            continue
        computed = _safe_eval(eval_src)
        if computed is None:
            continue
        tol = max(abs(res_canon) * 5e-3, 0.011)
        if abs(computed - res_canon) > tol:
            results.append(_ArithResult(rm.start(), False, "arith_wrong"))
            continue
        if not all(_operand_sourced(op, masked_small, pool, derived) for op in operands):
            results.append(_ArithResult(rm.start(), False, "operand_no_source"))
            continue
        if len(derived) < _DERIVED_CAP:
            derived.add(res_canon)
            derived.add(res_written)
        results.append(_ArithResult(rm.start(), True, ""))
    return results


# ----------------------------------------------------------------
# 打码
# ----------------------------------------------------------------

def _apply_mask(orig: str, violations: list[Violation], idx_map: list[int]) -> str:
    """从后往前包裹 (保持前面 index 有效); 数字+紧跟单位一起标; 区间号保留。

    原数字不删除 — 包进 〖…※〗 高亮标记, 由展示层渲染成醒目样式。
    """
    out = orig
    for v in sorted(violations, key=lambda x: -x.index):
        n_start = v.index + (1 if v.keep_dash else 0)
        if n_start >= len(idx_map) or v.unit_end > len(idx_map):
            continue
        o_start = idx_map[n_start]
        o_end = idx_map[v.unit_end - 1] + 1
        out = out[:o_start] + wrap_marked(out[o_start:o_end]) + out[o_end:]
    return out


# 消息面/新闻类章节: 数字来自联网搜索或模型记忆, 不在本地白名单 →
# 只记录不打码 (对齐 regime_alignment "边界宽内部严"; 设计文档 §8)
_NEWS_SECTION_RE = re.compile(r"消息面|新闻|资讯|舆情|联网|热点|传闻|公告")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")


def gate_text(text: str, pool: NumberPool, *, derived: set[float] | None = None,
              line_no: int = 1, mask_enabled: bool = True) -> GateResult:
    """对一段文本 (通常一行) 做全量核对: 提取 → 白名单/算式 → 打码。

    derived: 跨行携带的派生值集合 (算式结果), 调用方可传共享 set 实现链式。
    mask_enabled=False: 只记录违规不改文本 (消息面等宽松章节)。
    """
    if derived is None:
        derived = set()
    norm, idx_map = _normalize(text)
    masked_small = _mask_exemptions(norm)                 # 含小整数豁免: 主扫描用
    masked_full = _mask_exemptions(norm, small_int=False)  # 算式解析用 (要看到 "÷ 8")

    violations: list[Violation] = []
    if pool.empty_reason is not None:
        # fail-closed: 白名单为空/采集失败 → 全部非豁免数字打码, 绝不放行
        for tok in _extract(masked_small):
            violations.append(Violation(
                tok.raw, tok.value, line_no, "no_whitelist",
                tok.start, tok.unit_end, tok.neg_is_operator,
            ))
    else:
        arith_fails: dict[int, str] = {}
        for ar in _check_arithmetic(masked_full, masked_small, pool, derived):
            if not ar.ok:
                arith_fails[ar.res_start] = ar.reason
        for tok in _extract(masked_small):
            if tok.start in arith_fails:
                reason = arith_fails[tok.start]
            elif _in_derived(tok, derived):
                continue
            else:
                reason = _bound_reason(tok, masked_small, pool, derived) or ""
                if not reason:
                    continue
            violations.append(Violation(
                tok.raw, tok.value, line_no, reason,
                tok.start, tok.unit_end, tok.neg_is_operator,
            ))

    if not violations:
        return GateResult(text, 0, ())
    for v in violations:
        logger.info("number gate: line %d %r value=%s reason=%s",
                    v.line, v.raw, v.value, v.reason)
    if not mask_enabled:
        return GateResult(text, 0, tuple(violations))
    return GateResult(_apply_mask(text, violations, idx_map),
                      len(violations), tuple(violations))


def scan_text_numbers(text: str) -> list[tuple[float, str]]:
    """扫描文本中全部数字的"读法" (白名单采集用): [(数值, 规范单位)]。

    与闸门用同一条提取管线 (规范化 + 豁免), 保证核对两侧对称: 提示词里
    以豁免形态出现的数字 (日期等) 不会进池, 回答里同样形态也不会被核对。
    """
    norm, _ = _normalize(text)
    masked = _mask_exemptions(norm)
    return [(tok.value, tok.unit) for tok in _extract(masked)]


# ----------------------------------------------------------------
# 提示词配套规则 (显著降低打码率: 让模型主动把算式写出来)
# 刻意不含任何示例数字——系统提示词也会进白名单, 示例数字会留假放行口子
# ----------------------------------------------------------------

NUMBER_RULE_PROMPT = (
    "\n\n### 数字出处纪律\n"
    "你回答中的每个数字必须来自本轮提供的数据, 或由你写出的算式计算得到; "
    "引用计算结果时把算式一起写出来 (如: 涨幅 = (今收 − 昨收) ÷ 昨收)。"
    "找不到出处的数字不要写。系统会自动核对回答中数字的出处, "
    f"无出处的数字会被标为「{MARK_OPEN}…{MARK_CLOSE}」提醒用户。"
)

_FOOTER_TEMPLATE = (
    "> ※ 另有 {n} 处数字在本轮提供的数据里找不到出处, "
    f"也不是回答中算式算出来的, 已加「⚠」标注。"
)

# 页脚行前缀 — IM 机器人组装报告时用它识别并挪动页脚 (见 pop_gate_footer)
_FOOTER_LINE_PREFIX = "> ※ 另有"


def pop_gate_footer(text: str) -> tuple[str, str | None]:
    """从正文末尾摘出闸门页脚行 (blockquote), 供 IM 组装方挪到免责声明之后。

    页脚与免责声明都是 "> " 引用行: 挪到其后紧邻放置, markdown 渲染时合并
    进同一个引用块 (web 端由 MarkdownRenderer 的连续引用行合并逻辑处理)。
    返回 (去掉页脚的正文, 页脚行或 None); 正文末尾没有页脚时原样返回。
    """
    stripped = text.rstrip()
    idx = stripped.rfind("\n")
    last = stripped[idx + 1:]
    if not last.startswith(_FOOTER_LINE_PREFIX):
        return text, None
    return (stripped[:idx].rstrip() if idx >= 0 else ""), last


def is_enabled() -> bool:
    """闸门总开关 (用户偏好, 默认关 → 灰度)。模块限定读取便于测试打桩。"""
    from app.services import preferences

    return preferences.get_ai_number_gate_enabled()


def _delta_line(content: str) -> str:
    return json.dumps({"type": "delta", "content": content}, ensure_ascii=False)


class _StreamGate:
    """行缓冲状态机: delta 内容进缓冲, 完整行核对后流出。

    - 一个 chunk 可能是半个数字 ("+2." / "35%"), 必须持缓冲, 否则 12.34
      会被判成 12. 和 34 两个数;
    - 数字永远不会未经检查就 emit (无闪烁), 延迟 < 1 行, 流式 UX 保留;
    - 派生值集合跨行携带 (上一行算式结果可为下一行作证);
    - 章节标题跟踪: 消息面/新闻类章节只记录不打码。
    """

    def __init__(self, pool: NumberPool | Callable[[], NumberPool] | None):
        self._pool_src = pool
        self._pending = ""
        self._derived: set[float] = set()
        self._masked_total = 0
        self._line_no = 0
        self._section_relaxed = False
        # 已下发正文的最后一个字符 — 决定页脚前缀, 让页脚与紧邻的免责声明
        # 引用行合并进同一 blockquote (中间不能隔空行)
        self._tail = ""

    def _resolve_pool(self) -> NumberPool:
        if isinstance(self._pool_src, NumberPool):
            return self._pool_src
        if self._pool_src is None:
            return NumberPool(empty_reason="no_whitelist_provided")
        # callable: 助手链路的白名单随工具轮次累积, 每条完整行解析一次
        # (工厂内部按消息数缓存, 代价可控)
        return self._pool_src()

    def feed_line(self, line: str) -> list[str]:
        """处理一条 NDJSON 事件行, 返回应下发给消费方的事件行列表。"""
        try:
            evt = json.loads(line)
        except Exception:  # noqa: BLE001 - 非 JSON 行原样透传 (防御, 正常流不会)
            return [line]
        if not isinstance(evt, dict):
            return [line]
        etype = evt.get("type")
        if etype == "delta":
            self._pending += str(evt.get("content") or "")
            return self._drain_complete_lines()
        # 非 delta 事件: 先冲刷余量 (不留未核对文本), 再透传
        out = self._flush_pending()
        if etype == "done":
            if self._masked_total > 0:
                out.extend(self._footer_delta())
            evt["numbers_masked"] = self._masked_total
            # 归零防 finish() 再追加一次页脚 (done 与流结束都会走到)
            self._masked_total = 0
            out.append(json.dumps(evt, ensure_ascii=False))
            return out
        out.append(line)
        return out

    def finish(self) -> list[str]:
        """流结束: 冲刷余量 + 追加页脚说明 (done 事件缺失时的兜底)。"""
        out = self._flush_pending()
        if self._masked_total > 0:
            out.extend(self._footer_delta())
            self._masked_total = 0
        return out

    def _footer_delta(self) -> list[str]:
        """页脚作为引用行下发: 正文已以换行结尾则不再加空行, 保证与免责
        声明的 "> " 行紧邻 (markdown 合并为同一引用块)。"""
        prefix = "" if self._tail == "\n" else "\n"
        return [_delta_line(prefix + _FOOTER_TEMPLATE.format(n=self._masked_total))]

    def _drain_complete_lines(self) -> list[str]:
        out: list[str] = []
        while "\n" in self._pending:
            seg, self._pending = self._pending.split("\n", 1)
            out.extend(self._gate_segment(seg + "\n"))
        return out

    def _flush_pending(self) -> list[str]:
        if not self._pending:
            return []
        seg, self._pending = self._pending, ""
        return self._gate_segment(seg)

    def _gate_segment(self, seg: str) -> list[str]:
        self._line_no += 1
        heading = _HEADING_RE.match(seg)
        if heading:
            self._section_relaxed = bool(_NEWS_SECTION_RE.search(heading.group(1)))
        try:
            res = gate_text(seg, self._resolve_pool(), derived=self._derived,
                            line_no=self._line_no,
                            mask_enabled=not self._section_relaxed)
        except Exception:  # logger.exception 留痕, BLE001 不触发 (fail-closed 设计)
            # 闸门自身故障 → fail-closed: 该段全部打码 (打码是安全侧), 流不中断
            logger.exception("number gate failed on line %d, fail-closed masking",
                             self._line_no)
            try:
                res = gate_text(seg, NumberPool(empty_reason="gate_error"),
                                line_no=self._line_no)
            except Exception:  # 同上: 留痕后走最朴素兜底
                # 连兜底都失败 (极端场景): 用最朴素的正则替换, 保证不放行未核对数字
                logger.exception("number gate fallback failed on line %d", self._line_no)
                fallback = NUM_RE.sub(lambda m: wrap_marked(m.group(0)), seg)
                if fallback:
                    self._tail = fallback[-1]
                return [_delta_line(fallback)]
        self._masked_total += res.masked
        if res.text:
            self._tail = res.text[-1]
        return [_delta_line(res.text)] if res.text else []


async def gate_ndjson_stream(
    events: AsyncIterator[str],
    pool: NumberPool | Callable[[], NumberPool] | None,
) -> AsyncIterator[str]:
    """包装 NDJSON 事件流: 拦截 {"type":"delta"} 做数字核对, 其余事件透传。

    必须包在 service 层 *_stream() 的 delta 出口 (不能放 api 层): 定时复盘
    SSE (daily_pipeline) 与 IM 机器人 (wecom/dingtalk) 直接消费 service 流,
    api 层包装会漏掉这两条旁路 (设计文档 §3.2)。

    开关关闭时纯透传, 行为零变化。源流异常原样透传 (由各 analyzer 的外层
    try/except 收敛成 error 事件); 消费端停止时生成器自然关闭, 无后台任务。

    pool 可以是: NumberPool (analyzer 链路, 生成前一次采集) / 无参 callable
    (助手链路, 白名单随工具轮次累积) / None (fail-closed 全打码)。
    """
    if not is_enabled():
        async for line in events:
            yield line
        return
    gate = _StreamGate(pool)
    async for line in events:
        for out in gate.feed_line(line):
            yield out
    for out in gate.finish():
        yield out
