"""极简 Markdown -> 邮件 HTML 渲染器 (零依赖)。

每日复盘等推送正文是 Markdown, 邮件客户端直接展示纯文本会出现 `###`、`**` 等
原始标记 (见用户反馈截图)。这里只覆盖复盘报告实际用到的语法子集:
标题(h1-h4)、无序/有序列表、引用块、分隔线、加粗/斜体、段落。
不引入 markdown/mistune 等第三方库, 避免为单一渲染需求增加依赖面。

安全: 先对文本做 HTML 转义再套用标记, 正文中任何 `<script>` 等都会变成纯文本,
不存在注入面。样式全部内联 (多数邮件客户端会剥离 <head> 里的 <style>)。
"""
from __future__ import annotations

import html
import re

_FONT = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',"
    "'Hiragino Sans GB','Microsoft YaHei',sans-serif;"
)

_STYLE = {
    "h1": _FONT + "font-size:20px;font-weight:700;color:#1f2329;margin:0 0 14px;"
          "padding-bottom:10px;border-bottom:2px solid #e5e6eb;",
    "h2": _FONT + "font-size:17px;font-weight:700;color:#1f2329;margin:24px 0 10px;",
    "h3": _FONT + "font-size:15px;font-weight:600;color:#333;margin:20px 0 8px;",
    "h4": _FONT + "font-size:14px;font-weight:600;color:#4e5969;margin:16px 0 6px;",
    "p": "margin:8px 0;",
    "ul": "margin:8px 0;padding-left:22px;",
    "ol": "margin:8px 0;padding-left:22px;",
    "li": "margin:4px 0;",
    "hr": "border:none;border-top:1px solid #e5e6eb;margin:20px 0;",
    "blockquote": "margin:10px 0;padding:8px 14px;border-left:3px solid #d0d3d8;"
                  "background:#f7f8fa;color:#4e5969;",
    "strong": "font-weight:600;color:#1f2329;",
    "em": "font-style:italic;",
}

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_UL_RE = re.compile(r"^[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\d+[.)]\s+(.*)$")


_STRONG_OPEN = f'<strong style="{_STYLE["strong"]}">'
_EM_OPEN = f'<em style="{_STYLE["em"]}">'


def _inline(text: str) -> str:
    """转义后套用行内标记 (加粗/斜体)。"""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", _STRONG_OPEN + r"\1</strong>", escaped)
    escaped = re.sub(r"__(.+?)__", _STRONG_OPEN + r"\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", _EM_OPEN + r"\1</em>", escaped)
    return escaped


def _open(tag: str) -> str:
    return f"<{tag} style=\"{_STYLE[tag]}\">"


def markdown_to_html(markdown: str) -> str:
    """把 Markdown 文本渲染成带内联样式的完整 HTML 文档字符串。"""
    out: list[str] = []
    para: list[str] = []
    in_ul = in_ol = in_bq = False

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append(f"{_open('p')}{_inline(' '.join(para))}</p>")
            para = []

    def close_blocks() -> None:
        nonlocal in_ul, in_ol, in_bq
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False
        if in_bq:
            out.append("</blockquote>")
            in_bq = False

    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if not line:
            flush_para()
            close_blocks()
            continue

        m = _HEADING_RE.match(line)
        if m:
            flush_para()
            close_blocks()
            tag = f"h{len(m.group(1))}"
            out.append(f"{_open(tag)}{_inline(m.group(2))}</{tag}>")
            continue

        if _HR_RE.match(line):
            flush_para()
            close_blocks()
            out.append(f"<hr style=\"{_STYLE['hr']}\"/>")
            continue

        m = _UL_RE.match(line)
        if m:
            flush_para()
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if in_bq:
                out.append("</blockquote>")
                in_bq = False
            if not in_ul:
                out.append(_open("ul"))
                in_ul = True
            out.append(f"{_open('li')}{_inline(m.group(1))}</li>")
            continue

        m = _OL_RE.match(line)
        if m:
            flush_para()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if in_bq:
                out.append("</blockquote>")
                in_bq = False
            if not in_ol:
                out.append(_open("ol"))
                in_ol = True
            out.append(f"{_open('li')}{_inline(m.group(1))}</li>")
            continue

        if line.startswith(">"):
            flush_para()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_bq:
                out.append(_open("blockquote"))
                in_bq = True
            out.append(f"{_open('p')}{_inline(line.lstrip('>').strip())}</p>")
            continue

        # 普通段落: 相邻行合并, 遇到块级元素再 flush
        close_blocks()
        para.append(line)

    flush_para()
    close_blocks()

    body = "\n".join(out)
    return (
        "<!DOCTYPE html>\n<html>\n<head><meta charset=\"utf-8\"></head>\n"
        f"<body style=\"margin:0;padding:16px;background:#f2f3f5;{_FONT}\">\n"
        f"<div style=\"{_FONT}font-size:14px;line-height:1.75;color:#1f2329;"
        "background:#ffffff;padding:22px 26px;border-radius:8px;max-width:760px;\">\n"
        f"{body}\n</div>\n</body>\n</html>\n"
    )
