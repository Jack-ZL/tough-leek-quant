"""message_chunker 分段工具测试 — 上限约束 / 内容不丢 / 结构保持 / 多字节安全。"""
import re

from app.services.message_chunker import split_by_bytes, split_by_chars

_MARKER_RE = re.compile(r"\n\n\(\d+/\d+\)$")


def _strip_markers(parts: list[str]) -> list[str]:
    return [_MARKER_RE.sub("", p) for p in parts]


# ===== 基本行为 =====

def test_short_text_single_part_no_marker():
    assert split_by_chars("短文本", 100) == ["短文本"]
    assert split_by_bytes("短文本", 100) == ["短文本"]


def test_empty_text():
    assert split_by_chars("", 100) == [""]
    assert split_by_bytes(None or "", 100) == [""]


def test_exact_limit_not_split():
    text = "汉" * 100
    assert split_by_chars(text, 100) == [text]


# ===== 字符模式 =====

def test_chars_split_respects_limit_and_markers():
    text = "\n\n".join(f"## 第{i}节\n\n" + "汉" * 300 for i in range(10))
    parts = split_by_chars(text, 1000)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 1000
    n = len(parts)
    for i, p in enumerate(parts, 1):
        assert p.endswith(f"({i}/{n})")


def test_chars_split_preserves_all_content():
    """分段只是切分, 拼回去内容一字不丢 (段落边界以 \\n\\n 还原)。"""
    paras = [f"段落{i}:" + "内容" * 100 for i in range(12)]
    text = "\n\n".join(paras)
    parts = split_by_chars(text, 800)
    assert "\n\n".join(_strip_markers(parts)) == text


def test_oversized_single_line_hard_split():
    text = "x" * 5000  # 无段落/行边界, 只能硬切
    parts = split_by_chars(text, 1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(_strip_markers(parts)) == text


# ===== 字节模式 =====

def test_bytes_split_respects_limit():
    text = "\n\n".join("汉字内容" * 200 for _ in range(8))
    parts = split_by_bytes(text, 3000)
    assert len(parts) > 1
    for p in parts:
        assert len(p.encode("utf-8")) <= 3000


def test_bytes_split_never_cuts_multibyte_char():
    """硬切路径也不产生半个汉字 (每段可无损 UTF-8 解码, 拼接还原原文)。"""
    text = "汉" * 20000  # 60000 字节, 无边界, 强制按字节硬切
    parts = split_by_bytes(text, 3001)  # 非 3 倍数预算, 必然切在多字节中间
    for p in parts:
        assert len(p.encode("utf-8")) <= 3001
    assert "".join(_strip_markers(parts)) == text


# ===== Markdown 结构保持 =====

def test_code_fence_with_blank_lines_stays_in_one_part():
    fence = "```json\n{\n\n  \"a\": 1\n}\n```"  # 围栏内含空行, 不得当作段落分隔
    text = f"前文\n\n{fence}\n\n" + "尾部" * 500
    parts = split_by_chars(text, 600)
    assert any(fence in p for p in parts)


def test_table_rows_not_split_across_parts():
    header = "| 指标 | 值 |\n| --- | --- |"
    rows = "\n".join(f"| 行{i} | {i} |" for i in range(60))
    text = f"## 技术面\n\n{header}\n{rows}\n\n## 结尾\n\n综合研判内容。"
    parts = split_by_chars(text, 500)
    # 表格是连续行(一个段落块), 要么整块在一段, 要么按行降级装箱 — 但行本身不断裂
    joined = "\n\n".join(_strip_markers(parts))
    for i in range(60):
        assert f"| 行{i} | {i} |" in joined


def test_heading_paragraph_order_preserved():
    text = "\n\n".join(f"## 标题{i}\n\n正文{i}" for i in range(20))
    parts = split_by_chars(text, 500)
    rejoined = "\n\n".join(_strip_markers(parts))
    assert rejoined == text
    # 顺序: 标题i 始终在 正文i 之前
    for i in range(20):
        assert rejoined.index(f"标题{i}") < rejoined.index(f"正文{i}")
