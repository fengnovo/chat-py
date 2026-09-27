-- 放开 sandbox_provider 约束以支持本地 Docker 容器沙箱。
ALTER TABLE workspaces
  DROP CONSTRAINT IF EXISTS workspaces_sandbox_provider_check;

ALTER TABLE workspaces
  ADD CONSTRAINT workspaces_sandbox_provider_check
  CHECK (sandbox_provider IN ('e2b', 'docker'));
