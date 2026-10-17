"""Key / 凭据本地存储(§14)。

存储位置:`data/user_data/secrets.json`,权限 0600。
优先级:secrets.json > .env > 空(Free 模式)。

UI 改 Key 时只动这个文件,不动 .env。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from contextvars import ContextVar
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# (mtime_ns, size) 签名缓存 — 与 preferences.load 同模式。
# 实时行情每轮的 webhook/邮件判定会多次读 secrets, 避免每次全文件读+JSON 解析。
_cache: dict | None = None
_cache_sig: tuple[int, int] | None = None


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "secrets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def file_path() -> Path:
    """secrets.json 绝对路径 (设置页展示用)。

    该文件是 AI 与数据源 Key 的统一配置源, 支持用户用编辑器直接修改;
    load() 按 mtime 签名缓存失效, 外部保存后下一次请求即读取最新内容,
    无需重启。
    """
    return _path()


# ── AI 模型档案 (多模型切换) ────────────────────────────────────────────────
# 结构: secrets.json 中 ai_profiles 为 {id: {name, provider, base_url, api_key,
# model, ...}} 字典, ai_active_profile 指向当前档案 id。
# 关键约定: 扁平 ai_* 键 = 当前生效配置 — ai_provider/streaming/settings 等所有
# 运行时读取点只看扁平键 (零改动); "激活档案" = 把档案字段镜像写入扁平键。
# 档案库只是可一键切换的预设集合。旧格式 (数组 profiles / 纯扁平键) 在 load()
# 时内存归一化, 任意 save() 自动落盘为新结构。

# 档案字段名 ↔ 扁平键 ai_<field> 一一对应 (ai_round_checkpoint 是全局项, 不入档案)
_AI_PROFILE_FIELDS = (
    "provider", "protocol", "base_url", "api_key", "model", "user_agent",
    "max_output_tokens", "context_window", "web_search", "reasoning_effort",
    "codex_command", "codex_model", "codex_reasoning_effort",
)
AI_PROFILE_FIELDS = _AI_PROFILE_FIELDS  # 公开别名 (api/settings.py 档案同步用)

# 运行时临时覆盖 (ContextVar): 机器人 "@机器人 模型名称 标的" 指定档案单次分析等场景。
# 仅当前异步上下文生效, 不落盘、不改全局激活档案; load() 读取时合并到返回值。
_RUNTIME_OVERRIDE: ContextVar[dict | None] = ContextVar("secrets_runtime_override", default=None)


def set_runtime_override(flat: dict | None):
    """在当前上下文临时覆盖扁平 ai_* 配置; 返回 token 供 reset_runtime_override 恢复。"""
    return _RUNTIME_OVERRIDE.set(flat or None)


def reset_runtime_override(token) -> None:
    """恢复 set_runtime_override 保存的覆盖状态 (配合 try/finally 使用)。"""
    _RUNTIME_OVERRIDE.reset(token)


def flat_from_profile(profile: dict) -> dict:
    """档案字段镜像为扁平 ai_* 键; 档案缺失字段置空串 (同 activate 清残留语义)。"""
    return {
        f"ai_{field}": ("" if profile.get(field) is None else profile.get(field))
        for field in _AI_PROFILE_FIELDS
    }


def _slugify_profile_id(text: str) -> str:
    """从名称/模型名生成档案 id (小写, 非字母数字/汉字转 '-', 限长)。"""
    base = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", (text or "").strip().lower()).strip("-")
    return base[:48] or "model"


def _profile_from_flat(data: dict) -> dict:
    """从扁平 ai_* 键构造档案字段 (仅收集已设置的键)。"""
    prof: dict = {}
    for field in _AI_PROFILE_FIELDS:
        val = data.get(f"ai_{field}")
        if val is not None:
            prof[field] = val
    return prof


def _match_flat_profile(data: dict, profiles: dict) -> str:
    """按 (provider, base_url, model) 匹配扁平配置对应的档案 id; 无匹配返回 ''。"""
    if not any(data.get(f"ai_{f}") for f in ("base_url", "api_key", "model")):
        return ""
    flat = (
        str(data.get("ai_provider") or "openai_compat"),
        str(data.get("ai_base_url") or ""),
        str(data.get("ai_model") or ""),
    )
    for pid, prof in profiles.items():
        if (
            str(prof.get("provider") or "openai_compat"),
            str(prof.get("base_url") or ""),
            str(prof.get("model") or ""),
        ) == flat:
            return str(pid)
    return ""


def _normalize_ai_profiles(data: dict) -> dict:
    """把旧格式归一化为 {ai_profiles: dict, ai_active_profile: str} (内存视图)。

    兼容三种起点: ① 新格式 dict — 仅校验指针; ② 旧数组 (含 dev 项目遗留格式) —
    按 id/name/model 迁移; ③ 扁平键无对应档案且指针失效 — 捕获为 default 档案。
    指针解析: 扁平匹配优先 (激活/保存后必然匹配), 其次现有指针, 最后首个档案。
    任何异常都按原数据继续, 绝不让档案问题阻断密钥读取。
    """
    try:
        raw = data.get("ai_profiles")
        profiles: dict = {}
        if isinstance(raw, dict):
            profiles = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        elif isinstance(raw, list):
            for i, entry in enumerate(raw):
                if not isinstance(entry, dict):
                    continue
                pid = str(entry.get("id") or "").strip() or _slugify_profile_id(
                    str(entry.get("name") or entry.get("model") or f"profile-{i + 1}")
                )
                base, n = pid, 2
                while pid in profiles:
                    pid = f"{base}-{n}"
                    n += 1
                profiles[pid] = {k: v for k, v in entry.items() if k != "id"}
        data["ai_profiles"] = profiles

        # 指针解析: 扁平匹配优先 (激活/保存后必然匹配)。
        matched = _match_flat_profile(data, profiles)
        active = str(data.get("ai_active_profile") or "")
        flat_configured = any(data.get(f"ai_{f}") for f in ("base_url", "api_key", "model"))
        if not matched and flat_configured and active not in profiles:
            # 扁平已配置但库中无对应档案且指针失效 (旧文件迁移/手改扁平键) →
            # 捕获为 default 档案; 指针有效时不动库, 避免覆盖"已保存未激活"的编辑
            prof = _profile_from_flat(data)
            prof["name"] = str(profiles.get("default", {}).get("name") or data.get("ai_model") or "当前模型")
            profiles["default"] = prof
            matched = "default"
        data["ai_active_profile"] = matched or (
            active if active in profiles else next(iter(profiles), "")
        )
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("[secrets] AI 档案归一化失败, 按原数据继续: %s", exc)
        if not isinstance(data.get("ai_profiles"), dict):
            data["ai_profiles"] = {}
        data.setdefault("ai_active_profile", "")
        return data


def list_ai_profiles() -> list[dict]:
    """档案库列表 (含 id 字段, 按插入序)。"""
    profiles = load().get("ai_profiles") or {}
    return [{"id": str(k), **v} for k, v in profiles.items() if isinstance(v, dict)]


def get_active_ai_profile_id() -> str:
    """当前激活档案 id (未配置/指针失效时为 '')。"""
    return str(load().get("ai_active_profile") or "")


def upsert_ai_profile(profile_id: str, fields: dict) -> tuple[list[dict], str]:
    """新建/更新档案, 返回 (全量列表, 实际 id)。

    fields 中值为 None 的键跳过 (编辑时 api_key 不重填即保留原值); 新 id 冲突时
    自动加序号后缀; name 缺省用 model 兜底。
    """
    data = load()
    profiles: dict = dict(data.get("ai_profiles") or {})
    pid = (profile_id or "").strip()
    generated = not pid
    if generated:
        pid = _slugify_profile_id(str(fields.get("name") or fields.get("model") or ""))
        base, n = pid, 2
        while pid in profiles:  # 自动生成的 id 撞车 → 加序号后缀, 不覆盖已有档案
            pid = f"{base}-{n}"
            n += 1
    merged = {**profiles.get(pid, {}), **{k: v for k, v in fields.items() if v is not None and k != "id"}}
    merged.pop("id", None)
    if not merged.get("name"):
        merged["name"] = str(merged.get("model") or pid)
    profiles[pid] = merged
    save({"ai_profiles": profiles})
    return [{"id": str(k), **v} for k, v in profiles.items()], pid


def delete_ai_profile(profile_id: str) -> list[dict]:
    """删除档案; 若删的是激活档案则指针置空 (扁平配置不动, 运行时不受影响)。"""
    data = load()
    profiles = {k: v for k, v in (data.get("ai_profiles") or {}).items() if k != profile_id}
    updates: dict = {"ai_profiles": profiles}
    if str(data.get("ai_active_profile") or "") == profile_id:
        updates["ai_active_profile"] = ""
    save(updates)
    return [{"id": str(k), **v} for k, v in profiles.items()]


def activate_ai_profile(profile_id: str) -> dict | None:
    """激活档案: 字段镜像写入扁平 ai_* 键 + 更新指针。档案不存在返回 None。

    档案中缺失 (None) 的字段从扁平键清除, 避免上一个模型的残留值生效。
    """
    data = load()
    profile = (data.get("ai_profiles") or {}).get(profile_id)
    if not isinstance(profile, dict):
        return None
    updates: dict = {"ai_active_profile": str(profile_id)}
    stale: list[str] = []
    for field in _AI_PROFILE_FIELDS:
        val = profile.get(field)
        if val is None:
            stale.append(f"ai_{field}")
        else:
            updates[f"ai_{field}"] = val
    save(updates)
    if stale:
        clear(*stale)
    return {"id": str(profile_id), **profile}


def load() -> dict:
    """读取 secrets.json (带 mtime 签名缓存)。返回深拷贝, 调用方可自由修改。

    读取时同步做 AI 档案格式归一化 (旧数组/纯扁平键 → dict + 指针,
    见 _normalize_ai_profiles); 归一化结果随任意 save() 落盘为新结构。
    """
    global _cache, _cache_sig
    p = _path()
    try:
        sig = (p.stat().st_mtime_ns, p.stat().st_size)
    except OSError:
        return _with_runtime_override({})
    if _cache is not None and sig == _cache_sig:
        return _with_runtime_override(copy.deepcopy(_cache))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _with_runtime_override({})
    except Exception as e:  # noqa: BLE001
        logger.warning("secrets.json malformed: %s", e)
        return _with_runtime_override({})
    if not isinstance(data, dict):
        data = {}
    data = _normalize_ai_profiles(data)
    _cache = data
    _cache_sig = sig
    return _with_runtime_override(copy.deepcopy(_cache))


def _with_runtime_override(data: dict) -> dict:
    """合并当前上下文的运行时覆盖 (扁平 ai_* 键); 无覆盖时原样返回。"""
    ov = _RUNTIME_OVERRIDE.get()
    if ov:
        return {**data, **ov}
    return data


def save(updates: dict) -> dict:
    """合并写入(不会清掉未提及的字段)。返回新内容。"""
    current = load()
    current.update({k: v for k, v in updates.items() if v is not None})
    p = _path()
    atomic_write_text(
        p, json.dumps(current, indent=2, ensure_ascii=False), mode=0o600,
    )
    return current


def clear(*keys: str) -> dict:
    """清掉指定字段(留空清全部)。"""
    p = _path()
    if not p.exists():
        return {}
    if not keys:
        p.unlink()
        return {}
    current = load()
    for k in keys:
        current.pop(k, None)
    atomic_write_text(
        p, json.dumps(current, indent=2, ensure_ascii=False), mode=0o600,
    )
    return current


def get_tickflow_key() -> str:
    """取当前 TickFlow Key:secrets.json 优先,否则 .env。"""
    val = load().get("tickflow_api_key")
    if val:
        return val
    from app.config import settings
    return settings.tickflow_api_key or ""


def get_ai_key() -> str:
    """取当前 AI Key:secrets.json 优先,否则 .env。"""
    val = load().get("ai_api_key")
    if val:
        return val
    from app.config import settings
    return settings.ai_api_key or ""


def get_ai_config(key: str, default: str = "") -> str:
    """取 AI 配置项:secrets.json 优先,否则 config。"""
    val = load().get(key)
    if val:
        return val
    from app.config import settings
    return getattr(settings, key, default) or default


def get_ai_config_int(key: str, default: int) -> int:
    """取 AI 数值配置项 (如 ai_max_output_tokens): secrets.json 优先,否则 config。"""
    val = load().get(key)
    if val is not None:
        try:
            return int(val)
        except (TypeError, ValueError):
            logger.warning("ai config %s is not an int: %r", key, val)
    from app.config import settings
    return int(getattr(settings, key, default) or default)


def get_ai_config_bool(key: str, default: bool) -> bool:
    """取 AI 布尔配置项 (如 ai_web_search): secrets.json 优先,否则 config。"""
    val = load().get(key)
    if val is not None:
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)
    from app.config import settings
    return bool(getattr(settings, key, default))


def get_custom_webhook_secret() -> str:
    """Return the optional HMAC secret for the generic outbound webhook."""
    return str(load().get("custom_webhook_secret") or "")


def set_custom_webhook_secret(secret: str) -> str:
    """Persist or clear the generic outbound webhook HMAC secret."""
    value = (secret or "").strip()
    if value:
        save({"custom_webhook_secret": value})
    else:
        clear("custom_webhook_secret")
    return value


def get_email_smtp_password() -> str:
    """Return the SMTP password used by the email notification channel."""
    return str(load().get("email_smtp_password") or "")


def set_email_smtp_password(password: str) -> str:
    """Persist or clear the SMTP password used by email notifications."""
    value = password or ""
    if value:
        save({"email_smtp_password": value})
    else:
        clear("email_smtp_password")
    return value


def get_env_backed_secret(field: str, env_name: str) -> str:
    """取环境变量后备的密钥(插件 API Key 等):secrets.json 优先,否则环境变量。

    与 get_tickflow_key 同优先级语义:UI 写入 secrets.json 后即覆盖 .env。
    """
    val = load().get(field)
    if val:
        return str(val).strip()
    return os.environ.get(env_name, "").strip()


def mask(key: str, prefix: int = 4, suffix: int = 4) -> str:
    """脱敏显示。"""
    if not key:
        return ""
    if len(key) <= prefix + suffix:
        return "•" * len(key)
    return f"{key[:prefix]}{'•' * 6}{key[-suffix:]}"
