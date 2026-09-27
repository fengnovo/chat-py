---
name: agent-run-e2e-probe
description: Verify an agent run end-to-end in this repo by triggering it in the browser, polling Postgres run_events directly, then capturing the final UI state. Use when asked to test or verify a subagent spawn, reviewer retry, background run, or any agent flow against the running dev stack. Do not use for unit tests or static analysis.
---

# Agent Run E2E Probe

End-to-end verification for this chat platform. The canonical pattern is: **trigger run in browser → poll Postgres `run_events` directly → capture final UI state**. Browser automation budgets expire during long waits; do all polling through `psql` against the DB, and use the browser only to send the message and take final screenshots.

## 1. Confirm the stack is up

The dev stack must be running before any probe:

- web on `http://localhost:3020` (Next.js)
- api on `http://127.0.0.1:8002` (Fastify)
- worker logged `agent worker ready` with `(27 tools connected (shared))`

If it is not running, start it in the background:

```sh
pnpm dev   # from repo root
```

If ports 3020/8002 are occupied by stale processes, terminate the whole process tree (the `free-dev-ports.mjs` preflight, `concurrently`, pnpm filter processes, `src/server.ts`, `src/worker.ts`, and any tsx watch parents) then restart. Do not kill processes from other projects.

## 2. Trigger the run in the browser

Open `http://localhost:3020`, create or open a session, and send the test message. If an approval popup appears, click **仅批准这一次** (may repeat).

## 3. Get the run id and poll directly

The Postgres container is fixed: `chat-agent-postgres-1`, user `agent`, database `agent`.

```sh
RID=$(docker exec chat-agent-postgres-1 psql -U agent -d agent -tAc \
  "SELECT id FROM agent_runs ORDER BY created_at DESC LIMIT 1;")
```

Poll status until terminal (`completed` / `failed` / `cancelled`). Status `waiting_approval` means an approval popup is pending — have the browser approve it, then continue polling:

```sh
for i in $(seq 1 30); do
  s=$(docker exec chat-agent-postgres-1 psql -U agent -d agent -tAc \
    "SELECT status FROM agent_runs WHERE id='$RID'")
  echo "$i: $s"
  case "$s" in running|waiting_approval) sleep 20 ;; *) break ;; esac
done
```

## 4. Inspect the subagent event chain

The table is `run_events(run_id, seq, event_type, payload jsonb)`. Subagent events:

- `subagent.started` — has `attempt`, `background` (optional)
- `subagent.completed` — has `attempt`, `status` (`completed`/`failed`/`timeout`), `summary`, `toolCalls`, `durationMs`
- `subagent.reviewed` — has `attempt`, `passed`, `score`, `feedback`, `checklist[]` (each `{item, met}`)

```sh
docker exec chat-agent-postgres-1 psql -U agent -d agent -c \
"SELECT seq, event_type, payload->>'attempt' att, payload->>'passed' passed, \
 payload->>'score' score, payload->>'status' status, \
 jsonb_array_length(COALESCE(payload->'checklist','[]'::jsonb)) checks, \
 left(COALESCE(payload->>'feedback',''),100) feedback \
 FROM run_events WHERE run_id='$RID' AND event_type LIKE 'subagent%' ORDER BY seq;"
```

Expected shapes by phase:

- **P1 sync**: one `started` (no `background`), one `completed` (status `completed`), one `reviewed`, then `run.completed`.
- **P2 reviewer retry**: `started`/`completed`/`reviewed(passed=false)` repeating up to `SUBAGENT_MAX_ATTEMPTS` (default 3), then a final `reviewed` and `run.completed`. The card should end as "已完成（评审未达标）".
- **P3 background**: `started.background=true`, tool returns immediately, main agent gives a staged reply; after both complete, main agent auto-resumes and emits a final summary.

Terminal events to expect: `run.completed` (success), `run.failed`, `run.cancelled`. Approval interrupts surface as `approval.required` / `question.required` and put the run in `waiting_approval`.

## 5. Capture the final UI state

After the run reaches a terminal status, return to the browser (refresh if the SSE stream got stuck — cards rebuild from persisted message parts). Screenshot:

- the subagent card header (status text, attempt badge, background tag if any)
- the expanded card review trail (per-attempt score, passed, checklist, feedback)
- the main agent's final reply (quote the first ~150 chars)

Check the browser console for `run.failed` or other errors.

## 6. Judge success

A run passes the probe when:

- the expected subagent event sequence is present in `run_events`
- the terminal event is `run.completed` (not `run.failed` / `run.cancelled`)
- the UI card matches the expected phase state and the main agent reply is coherent
- no console errors

If `subagent.reviewed` is missing while `subagent.completed` is present with `status=completed`, the reviewer fail-opened. Check the worker log for `[subagent] reviewer skipped (fail-open): …` — that line records the real reason (model incompatibility, parse failure, timeout) instead of silently passing.
