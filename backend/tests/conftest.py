"""全局测试夹具。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_realtime_failover():
    """P1-12 layer2: 实时热备状态机是进程内单例, 每个测试前后清空,
    防止失败计数/熔断状态跨测试泄漏 (如某测试连打 3 轮失败把熔断打开,
    后续测试的主源被莫名切成备源)。"""
    from app.services.realtime_failover import get_failover

    get_failover().reset()
    yield
    get_failover().reset()
