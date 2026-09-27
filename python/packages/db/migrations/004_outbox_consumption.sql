ALTER TABLE run_dispatch_outbox
  ADD COLUMN IF NOT EXISTS consumed_at timestamptz;

CREATE INDEX IF NOT EXISTS run_dispatch_outbox_unconsumed_idx
  ON run_dispatch_outbox (published_at, created_at)
  WHERE published_at IS NOT NULL AND consumed_at IS NULL;
