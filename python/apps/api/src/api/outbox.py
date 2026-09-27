"""Outbox dispatcher — mirrors apps/api/src/outbox.ts."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from contracts import RUN_QUEUE_NAME, RunJob

logger = logging.getLogger(__name__)


class RunOutboxDispatcher:
    """Polls the outbox table and dispatches jobs to the queue."""

    def __init__(
        self,
        repository: Any,
        queue: Any,  # arq pool or similar
        *,
        poll_interval_ms: int = 500,
        batch_size: int = 50,
        lease_ms: int = 30_000,
        reconcile_interval_ms: int = 5_000,
        stale_after_ms: int = 30_000,
        queue_name: str = RUN_QUEUE_NAME,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._poll_interval_ms = poll_interval_ms
        self._batch_size = batch_size
        self._lease_ms = lease_ms
        self._reconcile_interval_ms = reconcile_interval_ms
        self._stale_after_ms = stale_after_ms
        self._queue_name = queue_name
        self._timer_task: asyncio.Task[None] | None = None
        self._draining: asyncio.Task[None] | None = None
        self._last_reconciled_at = 0.0
        self._stopped = False

    def start(self) -> None:
        if self._timer_task is not None:
            return
        self._stopped = False
        self._timer_task = asyncio.create_task(self._poll_loop())

    def wake(self) -> None:
        """Trigger an immediate drain if not already running."""
        if self._draining and not self._draining.done():
            return
        self._draining = asyncio.create_task(self._drain_safe())

    async def stop(self) -> None:
        self._stopped = True
        if self._timer_task:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
            self._timer_task = None
        if self._draining:
            try:
                await self._draining
            except Exception:
                pass

    async def drain_now(self) -> None:
        if self._draining and not self._draining.done():
            await self._draining
        await self._drain()

    async def _poll_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self._poll_interval_ms / 1000)
                if not self._stopped:
                    self.wake()
            except asyncio.CancelledError:
                break

    async def _drain_safe(self) -> None:
        try:
            await self._drain()
        except Exception:
            logger.exception("outbox drain failed")

    async def _drain(self) -> None:
        import time
        now_ms = time.monotonic() * 1000
        if now_ms - self._last_reconciled_at >= self._reconcile_interval_ms:
            self._last_reconciled_at = now_ms
            requeued = await self._repository.requeue_stale_dispatches(
                self._stale_after_ms, self._batch_size
            )
            if requeued > 0:
                logger.warning("stale outbox dispatches requeued: %d", requeued)

        while True:
            dispatches = await self._repository.claim_dispatches(
                self._batch_size, self._lease_ms
            )
            if not dispatches:
                return
            await asyncio.gather(*[self._publish_dispatch(d) for d in dispatches])
            if len(dispatches) < self._batch_size:
                return

    async def _publish_dispatch(self, dispatch: Any) -> None:
        try:
            job_data = dispatch.payload if hasattr(dispatch, "payload") else dispatch.get("payload", {})
            # Parse as RunJob to validate
            job = self._parse_run_job(job_data)
            await self._queue.enqueue_job(
                job.kind,
                job_data,
                _job_id=dispatch.id if hasattr(dispatch, "id") else dispatch.get("id"),
            )
            dispatch_id = dispatch.id if hasattr(dispatch, "id") else dispatch.get("id")
            await self._repository.mark_dispatch_published(dispatch_id)
        except Exception as e:
            dispatch_id = dispatch.id if hasattr(dispatch, "id") else dispatch.get("id")
            attempts = dispatch.attempts if hasattr(dispatch, "attempts") else dispatch.get("attempts", 1)
            retry_delay_ms = min(30_000, int(250 * math.pow(2, min(attempts - 1, 7))))
            await self._repository.reschedule_dispatch(
                dispatch_id,
                str(e),
                retry_delay_ms,
            )
            logger.warning(
                "outbox dispatch will be retried: dispatch_id=%s, error=%s, retry_delay_ms=%d",
                dispatch_id, e, retry_delay_ms,
            )

    def _parse_run_job(self, data: dict[str, Any]) -> RunJob:
        kind = data.get("kind", "start")
        if kind == "start":
            from contracts import RunJobStart
            return RunJobStart.model_validate(data)
        elif kind == "resume-approval":
            from contracts import RunJobResumeApproval
            return RunJobResumeApproval.model_validate(data)
        elif kind == "resume-question":
            from contracts import RunJobResumeQuestion
            return RunJobResumeQuestion.model_validate(data)
        raise ValueError(f"Unknown job kind: {kind}")
