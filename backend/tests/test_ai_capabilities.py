"""P1-15: 声明式模型能力表测试。

覆盖: 未登记模型保守默认、家族子串继承、host 限定怪癖、多规则合并、
describe() 前端契约, 以及 ai_provider 两处收编点 (web_search_supported /
_openai_kwargs thinking 禁用) 行为与建表前一致。
"""

from app.services.ai_capabilities import describe, lookup


class TestLookup:
    def test_unknown_model_conservative_defaults(self):
        # 未登记模型: 能力全开、无怪癖、registered=False (与建表前行为一致)
        caps = lookup("gpt-5.5", "https://api.openai.com/v1")
        assert caps.supports_web_search is True
        assert caps.default_thinking is False
        assert caps.thinking_shares_max_tokens is False
        assert caps.known_issues == ()
        assert caps.registered is False

    def test_empty_model_returns_defaults(self):
        assert lookup("").registered is False
        assert lookup("   ").supports_web_search is True

    def test_deepseek_family_no_web_search(self):
        # 家族级登记: 任何含 deepseek 的模型都不传 enable_search
        caps = lookup("deepseek-chat")
        assert caps.supports_web_search is False
        assert caps.registered is True
        assert caps.default_thinking is False  # chat 系不默认思考

    def test_reasoner_merges_family_and_own_rules(self):
        # deepseek-reasoner 同时命中 "deepseek" 与 "reasoner" 两条规则:
        # known_issues 累积, 字段后命中覆盖
        caps = lookup("deepseek-reasoner")
        assert caps.supports_web_search is False
        assert caps.thinking_shares_max_tokens is True
        assert len(caps.known_issues) == 2

    def test_v4_thinking_quirk_requires_official_host(self):
        # 官方 API: 默认思考 + 思考挤占预算 → 报告类任务会禁用思考
        caps = lookup("deepseek-v4-pro", "https://api.deepseek.com/v1")
        assert caps.default_thinking is True
        assert caps.thinking_shares_max_tokens is True
        # 第三方网关: host 不匹配 → 不下发 thinking 怪癖, 但家族级联网登记仍继承
        gw = lookup("deepseek-v4-pro", "https://gw.example.com/v1")
        assert gw.default_thinking is False
        assert gw.supports_web_search is False

    def test_match_is_case_and_space_insensitive(self):
        assert lookup("  DeepSeek-V4-Flash ", "https://API.DeepSeek.COM/v1").default_thinking is True

    def test_kimi_thinking_family_fixed_temperature(self):
        # 官方模型参数参考: k2.6 (思考 1.0/非思考 0.6)、k2.7-code、k3 温度固定,
        # 传其他值报 400 → 登记后请求不传 temperature
        for m in ("kimi-k2.6", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k3"):
            caps = lookup(m)
            assert caps.supports_temperature is False
            assert caps.registered is True
        # 非 thinking 系 kimi 未登记 → 保守默认 (行为与建表前一致)
        assert lookup("moonshot-v1-auto").supports_temperature is True


class TestDescribe:
    def test_registered_shape(self):
        d = describe("deepseek-v4-pro", "https://api.deepseek.com/v1")
        assert d["registered"] is True
        assert d["supports_web_search"] is False
        assert d["default_thinking"] is True
        assert isinstance(d["known_issues"], list) and d["known_issues"]

    def test_unregistered_shape(self):
        d = describe("qwen-plus")
        assert d == {
            "model": "qwen-plus",
            "registered": False,
            "supports_web_search": True,
            "supports_temperature": True,
            "default_thinking": False,
            "thinking_shares_max_tokens": False,
            "known_issues": [],
        }

    def test_settings_endpoint_contract(self):
        # API 契约: GET /ai/model-capabilities 直接透传 describe() 结果
        from app.api.settings import get_ai_model_capabilities

        d = get_ai_model_capabilities(model="deepseek-chat")
        assert d["registered"] is True and d["supports_web_search"] is False
        assert get_ai_model_capabilities()["registered"] is False  # 空模型名 → 未登记


class TestProviderIntegration:
    """ai_provider 两处收编点行为与建表前一致 (散点特判 → 查表)。"""

    def test_web_search_supported_uses_table(self, monkeypatch):
        from app import secrets_store
        from app.services import ai_provider

        stored = {"ai_provider": "openai_compat", "ai_model": "qwen-plus", "ai_web_search": True}
        monkeypatch.setattr(secrets_store, "load", lambda: stored)
        assert ai_provider.web_search_supported() is True
        # DeepSeek 官方聊天 API 无 enable_search → 能力表命中即回退
        stored["ai_model"] = "deepseek-chat"
        assert ai_provider.web_search_supported() is False

    def test_openai_kwargs_thinking_disable_via_table(self):
        from app.services import ai_provider

        # 官方 V4 + 报告类任务 → 显式禁用思考 (与旧 _DEEPSEEK_V4_MODELS 特判一致)
        kwargs = ai_provider._openai_kwargs(
            temperature=0.5,
            max_tokens=8192,
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/v1",
            prefer_final_answer=True,
        )
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        # 第三方网关不下发
        gw = ai_provider._openai_kwargs(
            temperature=0.5,
            max_tokens=8192,
            model="deepseek-v4-pro",
            base_url="https://gw.example.com/v1",
            prefer_final_answer=True,
        )
        assert "extra_body" not in gw

    def test_openai_kwargs_drops_temperature_for_fixed_temp_models(self):
        from app.services import ai_provider

        # 能力表命中 → 首发就不带 temperature (不再白耗一次 400 往返)
        kw = ai_provider._openai_kwargs(temperature=0.3, max_tokens=None, model="kimi-k3")
        assert "temperature" not in kw
        # 未登记模型照旧下发
        assert ai_provider._openai_kwargs(
            temperature=0.3, max_tokens=None, model="qwen3-max")["temperature"] == 0.3
        # temperature=None 仍不传 (与建表前一致)
        assert "temperature" not in ai_provider._openai_kwargs(
            temperature=None, max_tokens=None, model="qwen3-max")
