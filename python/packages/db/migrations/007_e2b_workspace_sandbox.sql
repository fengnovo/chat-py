ALTER TABLE workspaces
  ADD COLUMN IF NOT EXISTS sandbox_id text;

UPDATE workspaces
SET sandbox_provider = 'e2b'
WHERE sandbox_provider <> 'e2b';

ALTER TABLE workspaces
  ALTER COLUMN sandbox_provider SET DEFAULT 'e2b';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'workspaces_sandbox_provider_check'
  ) THEN
    ALTER TABLE workspaces
      ADD CONSTRAINT workspaces_sandbox_provider_check
      CHECK (sandbox_provider = 'e2b');
  END IF;
END $$;
