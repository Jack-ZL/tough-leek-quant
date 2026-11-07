"""webhook_adapter 复盘群推送超长分段发送测试 (monkeypatch 底层 POST, 不出网)。"""
import pytest

from app.services import webhook_adapter as wa

_WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "a" * 36
_DINGTALK_URL = "https://oapi.dingtalk.com/robot/send?access_token=" + "t" * 32


@pytest.fixture
def wecom_posts(monkeypatch):
    posted: list[dict] = []
    monkeypatch.setattr(wa, "_post_wecom", lambda url, payload, **kw: posted.append(payload) or True)
    return posted


@pytest.fixture
def dingtalk_posts(monkeypatch):
    posted: list[dict] = []
    monkeypatch.setattr(wa, "_post_dingtalk", lambda url, payload, secret="", **kw: posted.append(payload) or True)
    return posted


def _long_body(n_paras: int, para_chars: int) -> str:
    return "\n\n".join(f"## 第{i}节\n\n" + "汉" * para_chars for i in range(n_paras))


# ===== 企业微信 markdown (4096 字节上限, 复盘最常被卡的通道) =====

def test_wecom_markdown_short_single_post(wecom_posts):
    assert wa.send_wecom_markdown(_WECOM_URL, "每日复盘", "短正文") is True
    assert len(wecom_posts) == 1
    assert "## 每日复盘" in wecom_posts[0]["markdown"]["content"]


def test_wecom_markdown_long_splits_into_multiple_posts(wecom_posts):
    body = _long_body(10, 800)  # ~24000 字节, 远超单条 4096
    assert wa.send_wecom_markdown(_WECOM_URL, "每日复盘", body) is True
    assert len(wecom_posts) > 1
    for p in wecom_posts:
        assert len(p["markdown"]["content"].encode("utf-8")) <= wa._WECOM_MD_MAX_BYTES
    joined = "".join(p["markdown"]["content"] for p in wecom_posts)
    assert "## 每日复盘" in wecom_posts[0]["markdown"]["content"]  # 标题在首段
    for i in range(10):
        assert f"第{i}节" in joined  # 尾部章节不丢


def test_wecom_markdown_stops_on_failed_part(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, payload, **kw):
        calls.append(payload)
        return len(calls) < 2  # 第 2 段起失败

    monkeypatch.setattr(wa, "_post_wecom", fake_post)
    body = _long_body(10, 800)
    assert wa.send_wecom_markdown(_WECOM_URL, "每日复盘", body) is False
    assert len(calls) == 2  # 失败即停, 不继续刷段


def test_wecom_markdown_empty_body_still_sends_title(wecom_posts):
    # 正文为空时发出仅含标题的单段 (与改造前行为一致; 调用方保证正文非空)
    assert wa.send_wecom_markdown(_WECOM_URL, "每日复盘", "   ") is True
    assert len(wecom_posts) == 1


# ===== 钉钉 markdown (20000 字符上限) =====

def test_dingtalk_markdown_short_single_post(dingtalk_posts):
    assert wa.send_dingtalk_markdown(_DINGTALK_URL, "每日复盘", "短正文") is True
    assert len(dingtalk_posts) == 1
    assert "# 每日复盘" in dingtalk_posts[0]["markdown"]["text"]


def test_dingtalk_markdown_long_splits_into_multiple_posts(dingtalk_posts):
    body = _long_body(8, 4000)  # 32000+ 字符, 超单条 20000
    assert wa.send_dingtalk_markdown(_DINGTALK_URL, "每日复盘", body) is True
    assert len(dingtalk_posts) > 1
    for p in dingtalk_posts:
        assert len(p["markdown"]["text"]) <= wa._DINGTALK_MD_MAX_CHARS
        assert p["markdown"]["title"] == "每日复盘"  # 每段都带标题 (通知栏预览用)
    joined = "".join(p["markdown"]["text"] for p in dingtalk_posts)
    for i in range(8):
        assert f"第{i}节" in joined


def test_dingtalk_markdown_stops_on_failed_part(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, payload, secret="", **kw):
        calls.append(payload)
        return len(calls) < 2

    monkeypatch.setattr(wa, "_post_dingtalk", fake_post)
    assert wa.send_dingtalk_markdown(_DINGTALK_URL, "每日复盘", _long_body(8, 4000)) is False
    assert len(calls) == 2
