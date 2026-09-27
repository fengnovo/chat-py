ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS source_type text NOT NULL DEFAULT 'empty',
  ADD COLUMN IF NOT EXISTS source_ref text,
  ADD COLUMN IF NOT EXISTS source_revision text,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'projects_source_type_check'
  ) THEN
    ALTER TABLE projects
      ADD CONSTRAINT projects_source_type_check
      CHECK (source_type IN ('empty', 'git', 'upload'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS projects_tenant_updated_idx
  ON projects (tenant_id, updated_at DESC, id DESC);

ALTER TABLE agent_sessions
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

CREATE INDEX IF NOT EXISTS agent_sessions_user_updated_idx
  ON agent_sessions (tenant_id, user_id, updated_at DESC, id DESC)
  WHERE deleted_at IS NULL;
