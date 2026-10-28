"""通知投递策略 — 夜间免打扰闸门 (P0-4「通知别烦人、别泄密」)。

职责: 判断「此刻是否允许外部投递」。免打扰窗口内:
  - 暂停 webhook (飞书/企微/钉钉/第三方) 与邮件投递;
  - 暂停操作系统通知中心推送;
  - ★ 应用内 alerts.jsonl 落盘与 SSE 推送不受影响 — 告警不丢, 只是不吵,
    用户回到应用仍能看到免打扰期间产生的全部告警。

窗口语义 (与 preferences.notify_quiet_* 配套):
  - 起点含、终点不含: [start, end);
  - 支持跨天: start > end 表示 [start, 24:00) ∪ [00:00, end), 如 22:00→08:00;
  - start == end 视为「未配置窗口」(避免手滑把全天静音);
  - 配置非法 (非 HH:MM) → fail-open 放行并记 WARNING: 免打扰是舒适性功能,
    配置写错不该反过来吞掉告警 (与「安全侧 fail-closed」的数字闸门相反)。

时间口径: 统一用 cn_now() (Asia/Shanghai), 与交易日历/日期提醒同源,
避免服务器时区漂移导致「半夜」判断错位。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time as dtime

logger = logging.getLogger(__name__)

# HH:MM (24 小时制) 严格校验: 00:00–23:59
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(text: str) -> dtime | None:
    """解析 HH:MM 为 time 对象; 非法返回 None (调用方决定 fail-open/closed)。"""
    m = _HHMM_RE.match((text or "").strip())
    if not m:
        return None
    return dtime(hour=int(m.group(1)), minute=int(m.group(2)))


def in_quiet_hours(now: datetime | None = None) -> bool:
    """当前 (或给定) 时刻是否落在免打扰窗口内。

    Args:
        now: 可选时刻 (测试注入用); 默认取 cn_now()。
    """
    from app.market_time import cn_now
    from app.services import preferences

    if not preferences.get_notify_quiet_enabled():
        return False

    start = parse_hhmm(preferences.get_notify_quiet_start())
    end = parse_hhmm(preferences.get_notify_quiet_end())
    if start is None or end is None:
        # fail-open: 免打扰配置写错不应吞告警, 记 WARNING 便于排查
        logger.warning(
            "免打扰窗口配置非法 (start=%r end=%r), 本次放行推送",
            preferences.get_notify_quiet_start(), preferences.get_notify_quiet_end(),
        )
        return False
    if start == end:
        # 起终相同 = 全天静音, 几乎必然是手滑; 视为未配置窗口
        return False

    cur = (now if now is not None else cn_now()).time().replace(second=0, microsecond=0)
    if start < end:
        return start <= cur < end
    # 跨天窗口: 落在「当天起点之后」或「次日终点之前」都算窗口内
    return cur >= start or cur < end


def allow_push(now: datetime | None = None) -> bool:
    """外部投递闸门: True=允许推送, False=免打扰期内应跳过。

    quote_service 三处扇出 (规则 webhook / 模拟盘成交 webhook / 系统通知)
    在函数入口统一调用; 跳过时不打日志刷屏 (每轮轮询都会过一遍)。
    """
    return not in_quiet_hours(now)
