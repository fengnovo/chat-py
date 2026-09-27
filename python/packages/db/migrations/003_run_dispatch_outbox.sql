CREATE TABLE IF NOT EXISTS run_dispatch_outbox (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  job_kind text NOT NULL CHECK (
    job_kind IN ('start', 'resume-approval', 'resume-question')
  ),
  payload jsonb NOT NULL,
  attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  published_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS run_dispatch_outbox_pending_idx
  ON run_dispatch_outbox (available_at, created_at)
  WHERE published_at IS NULL;

CREATE INDEX IF NOT EXISTS run_dispatch_outbox_run_idx
  ON run_dispatch_outbox (tenant_id, run_id, created_at);
