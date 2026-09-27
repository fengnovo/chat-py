CREATE TABLE knowledge_bases (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  owner_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  name text NOT NULL,
  description text,
  visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'tenant')),
  embedding_profile_key text NOT NULL,
  embedding_model text NOT NULL,
  embedding_dim integer NOT NULL CHECK (embedding_dim > 0),
  collection_name text NOT NULL,
  chunk_size integer NOT NULL CHECK (chunk_size > 0),
  chunk_overlap integer NOT NULL CHECK (chunk_overlap >= 0 AND chunk_overlap < chunk_size),
  top_k integer NOT NULL CHECK (top_k > 0),
  max_hops integer NOT NULL CHECK (max_hops >= 0),
  graph_enabled boolean NOT NULL DEFAULT true,
  status text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE TABLE knowledge_documents (
  id uuid PRIMARY KEY,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name text NOT NULL,
  mime text NOT NULL,
  size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
  content_hash text NOT NULL,
  object_key text NOT NULL,
  status text NOT NULL,
  error_code text,
  error_message text,
  chunk_count integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
  indexed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE TABLE knowledge_chunks (
  id uuid PRIMARY KEY,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  text text NOT NULL,
  token_count integer NOT NULL CHECK (token_count >= 0),
  heading text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  vector_point_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, ordinal),
  UNIQUE (vector_point_id)
);

CREATE TABLE graph_entities (
  id uuid PRIMARY KEY,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  entity_key text NOT NULL,
  name text NOT NULL,
  type text NOT NULL,
  description text,
  chunk_ids uuid[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kb_id, document_id, entity_key)
);

CREATE TABLE graph_relationships (
  id uuid PRIMARY KEY,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  source_key text NOT NULL,
  target_key text NOT NULL,
  relation text NOT NULL,
  description text,
  chunk_ids uuid[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_index_jobs (
  id uuid PRIMARY KEY,
  kb_id uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('index', 'reindex', 'delete')),
  status text NOT NULL,
  progress integer NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  next_attempt_at timestamptz,
  lease_expires_at timestamptz,
  error_code text,
  error_message text,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_retrieval_logs (
  id uuid PRIMARY KEY,
  retrieval_id uuid NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  session_id uuid NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
  run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  kb_ids uuid[] NOT NULL,
  query text NOT NULL,
  top_k integer NOT NULL CHECK (top_k > 0),
  max_hops integer NOT NULL CHECK (max_hops >= 0),
  result_count integer NOT NULL CHECK (result_count >= 0),
  rerank_status text NOT NULL,
  citations jsonb NOT NULL,
  latency_ms integer NOT NULL CHECK (latency_ms >= 0),
  status text NOT NULL,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE agent_runs
  ADD COLUMN knowledge_base_ids uuid[] NOT NULL DEFAULT '{}';

CREATE INDEX knowledge_bases_tenant_status_idx ON knowledge_bases (tenant_id, status) WHERE deleted_at IS NULL;
CREATE INDEX knowledge_bases_tenant_owner_idx ON knowledge_bases (tenant_id, owner_user_id) WHERE deleted_at IS NULL;
CREATE INDEX knowledge_documents_tenant_kb_idx ON knowledge_documents (tenant_id, kb_id) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX knowledge_documents_active_content_hash_idx ON knowledge_documents (kb_id, content_hash) WHERE deleted_at IS NULL;
CREATE INDEX knowledge_chunks_tenant_kb_document_idx ON knowledge_chunks (tenant_id, kb_id, document_id, ordinal);
CREATE INDEX graph_entities_tenant_kb_key_idx ON graph_entities (tenant_id, kb_id, entity_key);
CREATE INDEX graph_entities_chunk_ids_gin_idx ON graph_entities USING GIN (chunk_ids);
CREATE INDEX graph_relationships_tenant_kb_source_idx ON graph_relationships (tenant_id, kb_id, source_key);
CREATE INDEX graph_relationships_tenant_kb_target_idx ON graph_relationships (tenant_id, kb_id, target_key);
CREATE INDEX graph_relationships_chunk_ids_gin_idx ON graph_relationships USING GIN (chunk_ids);
CREATE INDEX knowledge_index_jobs_tenant_status_idx ON knowledge_index_jobs (tenant_id, status, next_attempt_at);
CREATE INDEX knowledge_index_jobs_document_idx ON knowledge_index_jobs (tenant_id, document_id);
CREATE INDEX knowledge_retrieval_logs_tenant_run_idx ON knowledge_retrieval_logs (tenant_id, run_id, created_at DESC);
