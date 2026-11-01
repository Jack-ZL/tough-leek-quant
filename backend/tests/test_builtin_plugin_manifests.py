"""内置插件清单 usage_notes 必须是纯字符串列表 — 前端直接 .map() 渲染。

回归场景: tencent/plugin.yaml 里写了 `- 日K未接入: 腾讯日K无成交额字段...`,
YAML 把带 ": " 的列表项解析成 dict 而非字符串; 内置清单不走 packages.inspect_archive
的严格校验, dict 一路透传到 DataSources.tsx 的 usage_notes.map() → React 抛
"Objects are not valid as a React child" 整页崩溃。
修复: ① 清单里该条加引号; ② loader._manifest_status 出口统一过 _string_list 消毒。
"""
from __future__ import annotations

import yaml

from app.data_providers.custom import loader


def test_all_builtin_manifest_usage_notes_are_strings():
    """不变量锁: 扫描全部内置 plugin.yaml, usage_notes 每项必须是 str。"""
    manifests = sorted(loader.plugins_dir().glob("*/plugin.yaml"))
    assert manifests, "内置插件目录应至少有一个 plugin.yaml"
    for path in manifests:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        notes = data.get("usage_notes") or []
        assert isinstance(notes, list), f"{path.name}: usage_notes 必须是列表"
        for item in notes:
            assert isinstance(item, str), (
                f"{path.name}: usage_notes 含非字符串项 {item!r} "
                "(YAML 里 `键: 值` 形式的列表项要加引号, 否则解析成 dict)"
            )


def test_string_list_flattens_dict_and_drops_other_non_str():
    """dict 摊平成 "键: 值", 其余非字符串丢弃, None/空 → []。"""
    assert loader._string_list(None) == []
    assert loader._string_list([]) == []
    assert loader._string_list(["a", "b"]) == ["a", "b"]
    # dict: 保留信息而非静默丢掉 (前端仍能看到原文)
    assert loader._string_list([{"日K未接入": "请用 eastmoney"}]) == ["日K未接入: 请用 eastmoney"]
    # 多键 dict 全部摊平; 非字符串 (数字/None/嵌套) 丢弃
    assert loader._string_list([{"a": 1, "b": 2}]) == ["a: 1", "b: 2"]
    assert loader._string_list([1, None, "ok", ["x"]]) == ["ok"]


def test_manifest_status_sanitizes_dirty_usage_notes():
    """出口消毒: 脏清单经 _manifest_status 后 usage_notes 全为字符串。"""
    dirty = {
        "name": "demo",
        "usage_notes": ["正常", {"日K未接入": "备源用 eastmoney"}, 42, None],
    }
    status = loader._manifest_status(dirty, True, "ok")
    assert status["usage_notes"] == ["正常", "日K未接入: 备源用 eastmoney"]
    assert all(isinstance(n, str) for n in status["usage_notes"])
