"""markdown_html 渲染器测试: 覆盖复盘报告实际用到的语法子集与转义安全。"""
from __future__ import annotations

from app.services import markdown_html


def test_headings_and_paragraph_render_with_styles():
    html = markdown_html.markdown_to_html("# 标题一\n\n## 标题二\n\n正文段落")
    assert "<h1 style=" in html and "标题一</h1>" in html
    assert "<h2 style=" in html and "标题二</h2>" in html
    assert "<p style=" in html and "正文段落</p>" in html


def test_lists_hr_and_blockquote():
    md = "- 项一\n- 项二\n\n---\n\n> 引用内容\n\n1. 第一\n2. 第二"
    html = markdown_html.markdown_to_html(md)
    assert "<ul style=" in html and html.count("<li style=") == 4
    assert "<hr style=" in html
    assert "<blockquote style=" in html and "引用内容" in html
    assert "<ol style=" in html


def test_bold_and_italic_inline():
    html = markdown_html.markdown_to_html("上涨 **4680家**、下跌 *818家*")
    assert "<strong style=" in html and "4680家</strong>" in html
    assert "<em style=" in html and "818家</em>" in html


def test_adjacent_lines_merge_into_one_paragraph():
    html = markdown_html.markdown_to_html("第一行\n第二行\n\n新段落")
    assert html.count("<p style=") == 2
    assert "第一行 第二行</p>" in html


def test_html_is_escaped_to_prevent_injection():
    html = markdown_html.markdown_to_html("<script>alert(1)</script>\n\n**<b>bold</b>**")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>" not in html


def test_empty_input_returns_shell_document():
    html = markdown_html.markdown_to_html("")
    assert html.startswith("<!DOCTYPE html>")
    assert "<body" in html and "</html>" in html


def test_real_report_subset_renders_without_raw_markers():
    md = (
        "### 1. 🎯 一句话定调（1-2 句）\n"
        "今日市场呈现放量普涨结构。【市场状态: 偏强】\n"
        "\n"
        "---\n"
        "\n"
        "### 2. 📊 盘面总览\n"
        "- **指数表现**: 上证指数收涨 **+1.08%**。\n"
    )
    html = markdown_html.markdown_to_html(md)
    assert "###" not in html
    assert "**" not in html
    assert "<h3 style=" in html and "<strong style=" in html
