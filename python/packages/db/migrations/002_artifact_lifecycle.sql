ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'ready')),
  ADD COLUMN IF NOT EXISTS uploaded_at timestamptz;

CREATE INDEX IF NOT EXISTS artifacts_tenant_status_idx
  ON artifacts (tenant_id, status, created_at DESC);
