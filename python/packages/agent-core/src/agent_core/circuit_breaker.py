"""熔断器：连续失败超过阈值后打开，冷却期后进入半开试探。"""

from __future__ import annotations

import time


def _now_ms() -> float:
    # 与 TS Date.now() 对齐：墙钟毫秒。半开冷却属于人工运维时间尺度，墙钟足够。
    return time.time() * 1000


class _CircuitState:
    __slots__ = ("failures", "opened_at", "half_open_successes")

    def __init__(self) -> None:
        self.failures = 0
        self.opened_at: float | None = None
        self.half_open_successes = 0


class InMemoryCircuitBreakerStore:
    """进程内熔断器存储：单 Worker 默认实现。"""

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_after_ms: float = 30_000,
        success_threshold: int = 2,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_after_ms = reset_after_ms
        self._success_threshold = success_threshold
        self._states: dict[str, _CircuitState] = {}

    async def allows(self, key: str) -> bool:
        state = self._state(key)
        if state.opened_at is None:
            return True
        return _now_ms() - state.opened_at >= self._reset_after_ms

    async def record_success(self, key: str) -> None:
        state = self._state(key)
        if state.opened_at is not None and _now_ms() - state.opened_at >= self._reset_after_ms:
            state.half_open_successes += 1
            if state.half_open_successes < self._success_threshold:
                return
        state.failures = 0
        state.opened_at = None
        state.half_open_successes = 0

    async def record_failure(self, key: str) -> None:
        state = self._state(key)
        state.failures += 1
        state.half_open_successes = 0
        if state.failures >= self._failure_threshold:
            state.opened_at = _now_ms()

    def _state(self, key: str) -> _CircuitState:
        current = self._states.get(key)
        if current is not None:
            return current
        created = _CircuitState()
        self._states[key] = created
        return created
