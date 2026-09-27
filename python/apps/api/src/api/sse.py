"""SSE stream helpers — mirrors apps/api/src/sse.ts and chat-stream.ts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

from contracts import run_events_channel
from fastapi import Request
from fastapi.responses import StreamingResponse

HEARTBEAT_INTERVAL_S = 15


def parse_cursor(request: Request) -> int:
    query = request.query_params
    header = request.headers.get("last-event-id", "")
    raw = query.get("cursor") or query.get("startIndex") or (header.split(":")[-1] if header else "0")
    try:
        parsed = int(raw)
        return parsed if parsed >= 0 else 0
    except (ValueError, TypeError):
        return 0


def sse_frame(event: dict[str, Any]) -> str:
    run_id = event.get("runId", "")
    seq = event.get("seq", 0)
    return f"id: {run_id}:{seq}\nevent: agent\ndata: {json.dumps(event)}\n\n"


def workflow_frame(chunk: dict[str, Any]) -> str:
    return f"data: {json.dumps(chunk)}\n\n"


def create_coalesced_runner(
    task: Callable[[], Coroutine[Any, Any, bool]],
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Merge concurrent calls: if called while running, mark pending and re-run after."""
    running = False
    pending = False

    async def runner() -> None:
        nonlocal running, pending
        if running:
            pending = True
            return
        running = True
        try:
            while True:
                pending = False
                finished = await task()
                if finished or not pending:
                    return
        finally:
            running = False

    return runner


def chunks_from_events(run_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert persisted agent events to UI message stream chunks (Vercel AI SDK format)."""
    if not events:
        return []

    message_id = f"message-{run_id}"
    text_id = f"text-{run_id}"
    chunks: list[dict[str, Any]] = [
        {"type": "start", "messageId": message_id, "messageMetadata": {"runId": run_id}},
    ]
    text_started = False
    finished = False

    for event in events:
        event_type = event.get("type")

        if event_type == "assistant.delta":
            if not text_started:
                chunks.append({"type": "text-start", "id": text_id})
                text_started = True
            chunks.append({"type": "text-delta", "id": text_id, "delta": event["text"]})
            continue

        agent_data = event

        if event_type == "retrieval.completed":
            # Keep the process trace, while also persisting an auditable citation part.
            citations_data = [
                {
                    "chunkId": c.get("chunkId"),
                    **({"kbId": c["kbId"]} if c.get("kbId") else {}),
                    "documentId": c.get("documentId"),
                    "documentName": c.get("documentName"),
                    "ordinal": c.get("ordinal"),
                    **({"heading": c["heading"]} if c.get("heading") else {}),
                    "score": c.get("score"),
                    "via": c.get("via"),
                    **({"images": c["images"]} if c.get("images") else {}),
                }
                for c in event.get("citations", [])
            ]
            chunks.append({
                "type": "data-citations",
                "data": {**event, "citations": citations_data},
                "transient": False,
            })
            agent_data = {k: v for k, v in event.items() if k != "citations"}

        if event_type in ("subagent.started", "subagent.completed", "subagent.reviewed"):
            chunks.append({"type": "data-subagent", "data": event, "transient": False})

        chunks.append({"type": "data-agent", "data": agent_data, "transient": True})

        if not finished and event_type in ("run.completed", "run.failed", "run.cancelled"):
            if text_started:
                chunks.append({"type": "text-end", "id": text_id})
            chunks.append({
                "type": "finish",
                "finishReason": "stop" if event_type == "run.completed" else "error",
            })
            finished = True

    return chunks


def requested_start(request: Request, total: int) -> int:
    if request.headers.get("x-page-resume") == "1":
        return 0
    raw = request.query_params.get("startIndex", "0")
    try:
        parsed = int(raw)
    except (ValueError, TypeError):
        return 0
    return max(0, total + parsed) if parsed < 0 else min(parsed, total)


async def stream_agent_events(
    request: Request,
    run_id: str,
    repository: Any,
    auth: Any,
    stream_subscriptions: Any,
    observability: Any = None,
) -> StreamingResponse:
    """SSE endpoint for run events — mirrors sse.ts."""
    run = await repository.get_run(auth, run_id)
    if not run:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})

    telemetry = observability.start_sse("events") if observability else None
    cursor = parse_cursor(request)
    closed = False
    unsubscribe: Callable[[], None] | None = None
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def event_stream():  # type: ignore[return]
        nonlocal closed, unsubscribe, cursor

        def do_close(reason: str) -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            if unsubscribe:
                unsubscribe()
            if telemetry:
                telemetry.finish(reason)
            queue.put_nowait(None)

        async def flush() -> bool:
            nonlocal cursor
            if closed:
                return True
            try:
                while True:
                    events = await repository.list_events(auth, run_id, cursor)
                    if closed:
                        return True
                    if not events:
                        break
                    for event in events:
                        if event.seq <= cursor:
                            continue
                        cursor = event.seq
                        if telemetry:
                            telemetry.first_byte()
                        queue.put_nowait(sse_frame(event.model_dump(by_alias=True)))
                    if len(events) < 500:
                        break
                latest = await repository.get_run(auth, run_id)
                if latest and latest.status in ("completed", "failed", "cancelled"):
                    do_close("server")
                    return True
                return False
            except Exception:
                do_close("error")
                return True

        coalesced_flush = create_coalesced_runner(flush)

        try:
            unsubscribe = await stream_subscriptions.subscribe(
                run_events_channel(run_id),
                lambda: asyncio.create_task(coalesced_flush()),
                lambda e: do_close("error"),
            )
            if closed:
                return

            # Initial flush
            await coalesced_flush()

            while not closed:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_S)
                except asyncio.TimeoutError:
                    if not closed:
                        yield ": heartbeat\n\n"
                    continue
                if item is None:
                    break
                yield item

        except Exception:
            do_close("error")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-agent-run-id": run_id,
        },
    )


async def stream_workflow_run(
    request: Request,
    run_id: str,
    repository: Any,
    auth: Any,
    stream_subscriptions: Any,
    observability: Any = None,
) -> StreamingResponse:
    """SSE endpoint for workflow run — mirrors chat-stream.ts."""
    run = await repository.get_run(auth, run_id)
    if not run:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail={"error": "run_not_found"})

    telemetry = observability.start_sse("chat") if observability else None
    initial_events = await repository.list_events(auth, run_id, 0, 100_000)
    initial_chunks = chunks_from_events(run_id, [e.model_dump(by_alias=True) for e in initial_events])
    chunk_cursor = requested_start(request, len(initial_chunks))
    closed = False
    unsubscribe: Callable[[], None] | None = None
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def event_stream():  # type: ignore[return]
        nonlocal closed, chunk_cursor, unsubscribe

        def do_close(reason: str) -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            if unsubscribe:
                unsubscribe()
            if telemetry:
                telemetry.finish(reason)
            queue.put_nowait(None)

        async def flush() -> bool:
            nonlocal chunk_cursor
            if closed:
                return True
            try:
                events = await repository.list_events(auth, run_id, 0, 100_000)
                if closed:
                    return True
                chunks = chunks_from_events(run_id, [e.model_dump(by_alias=True) for e in events])
                for chunk in chunks[chunk_cursor:]:
                    if telemetry:
                        telemetry.first_byte()
                    queue.put_nowait(workflow_frame(chunk))
                    chunk_cursor += 1
                if any(c.get("type") == "finish" for c in chunks):
                    do_close("server")
                    return True
                return False
            except Exception:
                do_close("error")
                return True

        coalesced_flush = create_coalesced_runner(flush)

        try:
            unsubscribe = await stream_subscriptions.subscribe(
                run_events_channel(run_id),
                lambda: asyncio.create_task(coalesced_flush()),
                lambda e: do_close("error"),
            )
            if closed:
                return

            # Initial flush
            await coalesced_flush()

            while not closed:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_S)
                except asyncio.TimeoutError:
                    if not closed:
                        yield ": heartbeat\n\n"
                    continue
                if item is None:
                    break
                yield item

        except Exception:
            do_close("error")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-workflow-run-id": run_id,
            "x-workflow-stream-tail-index": str(max(len(initial_chunks) - 1, 0)),
            "x-vercel-ai-ui-message-stream": "v1",
        },
    )
