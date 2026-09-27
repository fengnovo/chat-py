ALTER TABLE agent_sessions
  ADD COLUMN IF NOT EXISTS approval_mode text NOT NULL DEFAULT 'manual';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'agent_sessions_approval_mode_check'
  ) THEN
    ALTER TABLE agent_sessions
      ADD CONSTRAINT agent_sessions_approval_mode_check
      CHECK (approval_mode IN ('manual', 'session'));
  END IF;
END $$;
