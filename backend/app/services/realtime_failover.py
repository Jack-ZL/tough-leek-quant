"""P1-12 layer2: 实时行情热备 (运行时故障切换)。

为什么只有实时行情做热备: 实时快照是无状态读取, 切源零污染风险
(日K/复权/分钟等落库任务中途换源会造成数据混杂, 由 layer3 任务级重跑兜底)。

机制 = 熔断 + 粘滞切换 + 半开探测:
  1. 主源连续失败 N 轮 (默认 3) → 熔断打开, 粘滞切到备源;
  2. 熔断期内 (默认 300s) 每轮都走备源, 完全不碰主源 — 给主源恢复时间,
     也避免对着已封禁/宕机的接口继续打请求;
  3. 冷却到期 → 半开: 下一轮放主源探测一次, 成功 → 熔断关闭切回主源,
     失败 → 重新熔断再冷却一个周期;
  4. 每次切换/探测/切回都打 INFO 日志, 熔断期间每轮也打一条 — 不静默。

备源选择 (preferences.realtime_backup_provider):
  - "auto" (默认): find_complement_provider 自动挑免费内置源, 优先腾讯
    (东财 push2 高频轮询易触发 IP 封禁, 且封禁池与日K同步共享, 不适合当热备);
  - "off": 关闭热备;
  - 具体源名: 用户指定 (需可用且声明 realtime)。

线程模型: quote_service 轮询线程单写, API 线程只读 status();
用一把小锁保护状态字段即可 (无长临界区)。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# 连续失败多少轮后熔断 (轮询间隔 1~6s, 3 轮 ≈ 3~18s 内确认主源真挂了)
FAILURE_THRESHOLD = 3
# 熔断冷却时长 (秒), 到期进入半开探测
COOLDOWN_SECONDS = 300.0


class RealtimeFailover:
    """实时行情主备切换状态机 (进程内单例, 见 get_failover)。"""

    def __init__(
        self,
        failure_threshold: int = FAILURE_THRESHOLD,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # 状态字段 (全部在 _lock 内读写)
        self._primary: str | None = None  # 最近一次 resolve 的主源
        self._backup: str | None = None  # 当前选定的备源 (None = 无热备)
        self._failures = 0  # 主源连续失败计数
        self._opened_at: float | None = None  # 熔断打开时刻 (None = 未熔断)
        self._half_open = False  # 冷却到期后是否处于半开探测轮

    # ===== 备源解析 =====

    def _resolve_backup(self, primary: str) -> str | None:
        """按偏好解析备源: off → None; auto → 免费内置源(优先腾讯); 指定名 → 校验可用性。"""
        from app.data_providers import custom as custom_sources
        from app.services import preferences

        pref = preferences.get_realtime_backup_provider()
        if pref == "off":
            return None
        if pref != "auto":
            # 用户指定备源: 必须不是主源自己、仍可用且声明 realtime, 否则视为无热备
            # (不擅自换成别的源 — 指定即意图)
            if pref != primary and custom_sources.provider_has_dataset(pref, "realtime"):
                return pref
            return None
        # auto: 腾讯优先 (理由见模块 docstring)
        backup = custom_sources.find_complement_provider(
            "realtime", exclude=primary, prefer=("tencent",)
        )
        # 防御: 备源不得是主源自己 (测试桩可能忽略 exclude)
        return None if backup == primary else backup

    # ===== 每轮取数前: 决定这一轮用谁 =====

    def resolve(self, primary: str) -> str:
        """返回本轮实际取数源名。主源健康/无备源时原样返回 primary。"""
        with self._lock:
            # 主源变了 (用户在设置页切换) → 旧熔断状态作废, 从头计;
            # 首次调用 (_primary=None) 不算切换, 否则会把已累积的失败清零
            if self._primary is not None and primary != self._primary:
                if self._opened_at is not None:
                    logger.info(
                        "实时热备: 主源已切换 %s → %s, 熔断状态重置", self._primary, primary
                    )
                self._reset_locked()
            self._primary = primary
            # 备源每轮重解析: 偏好改动/插件加载状态变化即刻生效, 不吃陈旧缓存
            # (字典级开销, 相对每轮网络取数可忽略)
            self._backup = self._resolve_backup(primary)

            if self._opened_at is None:
                return primary  # 未熔断 → 主源
            elapsed = self._clock() - self._opened_at
            if elapsed < self._cooldown:
                # 熔断期内粘滞走备源; 备源中途失效 (被卸载等) 则只能继续硬着头皮用主源
                return self._backup or primary
            # 冷却到期 → 半开: 这一轮放主源探测
            self._half_open = True
            logger.info(
                "实时热备: 冷却 %.0fs 到期, 本轮探测主源 %s (备源 %s 暂代中)",
                self._cooldown,
                primary,
                self._backup or "无",
            )
            return primary

    # ===== 每轮取数后: 上报成败 =====

    def on_success(self, source: str) -> None:
        """本轮取数成功。半开探测成功 → 关闭熔断切回主源。"""
        with self._lock:
            self._failures = 0
            if self._half_open:
                self._half_open = False
                self._opened_at = None
                logger.info("实时热备: 主源 %s 探测成功, 熔断关闭, 切回主源", source)

    def on_failure(self, source: str) -> None:
        """本轮取数失败 (异常/空快照)。累计到阈值 → 熔断切备源。"""
        with self._lock:
            if self._half_open:
                # 半开探测失败 → 重新熔断, 再冷却一个周期
                self._half_open = False
                self._opened_at = self._clock()
                logger.warning(
                    "实时热备: 主源 %s 探测失败, 重新熔断, 备源 %s 继续顶 %.0fs",
                    source,
                    self._backup or "无",
                    self._cooldown,
                )
                return
            if self._opened_at is not None:
                # 熔断期内失败的是备源 → 只记日志 (没有第三源可切, 等半开探测主源)
                if source != self._primary:
                    logger.warning("实时热备: 备源 %s 本轮也失败, 等待主源探测窗口", source)
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = self._clock()
                self._failures = 0
                if self._backup:
                    logger.warning(
                        "实时热备: 主源 %s 连续 %d 轮失败, 熔断打开 → 粘滞切换备源 %s (冷却 %.0fs)",
                        source,
                        self._threshold,
                        self._backup,
                        self._cooldown,
                    )
                else:
                    logger.warning(
                        "实时热备: 主源 %s 连续 %d 轮失败, 但无可用备源 (偏好 off 或无免费内置源), 仅记录",
                        source,
                        self._threshold,
                    )
            else:
                logger.info(
                    "实时热备: 主源 %s 本轮失败 (%d/%d)", source, self._failures, self._threshold
                )

    # ===== 对外状态 (API / 前端展示) =====

    def status(self) -> dict:
        """当前热备状态快照, 供 /capability-matrix 透出给前端。"""
        with self._lock:
            opened = self._opened_at is not None
            remaining = 0.0
            if opened:
                remaining = max(0.0, self._cooldown - (self._clock() - self._opened_at))
            if not opened:
                state = "closed"
            elif self._half_open:
                state = "half_open"
            else:
                state = "open"
            return {
                "state": state,  # closed=主源健康 / open=备源顶替中 / half_open=探测主源中
                "primary": self._primary,
                "backup": self._backup,
                # 当前实际生效源: 熔断中且备源可用 → 备源, 否则主源
                "active": self._backup if (opened and not self._half_open and self._backup) else self._primary,
                "failures": self._failures,
                "threshold": self._threshold,
                "cooldown_seconds": self._cooldown,
                "cooldown_remaining": round(remaining, 1),
            }

    def reset(self) -> None:
        """清空状态 (备源偏好变更等场景调用, 下一轮 resolve 重新解析)。"""
        with self._lock:
            self._reset_locked()
            self._primary = None
            self._backup = None

    def _reset_locked(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open = False


# ===== 进程内单例 =====
# quote_service 轮询线程与 settings API 线程共享同一份状态;
# 测试里可直接 new RealtimeFailover() 注入假时钟。

_FAILOVER: RealtimeFailover | None = None
_FAILOVER_LOCK = threading.Lock()


def get_failover() -> RealtimeFailover:
    global _FAILOVER
    with _FAILOVER_LOCK:
        if _FAILOVER is None:
            _FAILOVER = RealtimeFailover()
        return _FAILOVER
