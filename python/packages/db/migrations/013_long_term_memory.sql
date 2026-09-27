CREATE TABLE IF NOT EXISTS agent_memories (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_id uuid REFERENCES projects(id) ON DELETE CASCADE,
  assistant_key text NOT NULL DEFAULT 'chat',
  scope text NOT NULL,
  kind text NOT NULL,
  content text NOT NULL,
  normalized_key text NOT NULL,
  importance double precision NOT NULL DEFAULT 0.5,
  confidence double precision NOT NULL DEFAULT 0.5,
  status text NOT NULL DEFAULT 'active',
  source_session_id uuid REFERENCES agent_sessions(id) ON DELETE SET NULL,
  source_run_id uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
  supersedes_id uuid REFERENCES agent_memories(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_accessed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (importance >= 0 AND importance <= 1),
  CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS agent_memories_active_key_idx
  ON agent_memories (tenant_id, user_id, assistant_key, scope, normalized_key)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS agent_memories_scope_idx
  ON agent_memories (tenant_id, user_id, assistant_key, scope, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_jobs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id uuid NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
  run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'queued',
  attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, run_id)
);

CREATE INDEX IF NOT EXISTS memory_jobs_claim_idx
  ON memory_jobs (status, available_at, created_at);
