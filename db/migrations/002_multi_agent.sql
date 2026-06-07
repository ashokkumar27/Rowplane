DO $$
BEGIN
  CREATE TYPE agent_task_status AS ENUM (
    'queued',
    'thinking',
    'needs_tool',
    'tool_running',
    'waiting_approval',
    'waiting_child',
    'completed',
    'failed',
    'blocked'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS agents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  name text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]*$'),
  role text NOT NULL CHECK (length(role) > 0),
  instructions text NOT NULL CHECK (length(instructions) > 0),
  model text NOT NULL DEFAULT 'unset',
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS agent_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  agent_id uuid NOT NULL,
  parent_task_id uuid,
  status agent_task_status NOT NULL DEFAULT 'queued',
  input jsonb NOT NULL DEFAULT '{}'::jsonb,
  output jsonb,
  error text,
  iteration_count integer NOT NULL DEFAULT 0 CHECK (iteration_count >= 0),
  max_iterations integer NOT NULL DEFAULT 10 CHECK (max_iterations > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, id),
  FOREIGN KEY (tenant_id, parent_task_id) REFERENCES agent_tasks (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS agent_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  from_task_id uuid,
  to_task_id uuid,
  message_type text NOT NULL CHECK (message_type IN (
    'delegation',
    'task_result',
    'critique',
    'observation',
    'final_candidate'
  )),
  content jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, from_task_id) REFERENCES agent_tasks (tenant_id, id),
  FOREIGN KEY (tenant_id, to_task_id) REFERENCES agent_tasks (tenant_id, id)
);

ALTER TABLE tool_executions
  ADD COLUMN IF NOT EXISTS task_id uuid;

ALTER TABLE approval_requests
  ADD COLUMN IF NOT EXISTS task_id uuid;

DO $$
BEGIN
  ALTER TABLE tool_executions
    ADD CONSTRAINT tool_executions_task_fk
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_tasks (tenant_id, id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
  ALTER TABLE approval_requests
    ADD CONSTRAINT approval_requests_task_fk
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_tasks (tenant_id, id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_agents_tenant_name
  ON agents (tenant_id, name);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_run_status
  ON agent_tasks (tenant_id, run_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_parent
  ON agent_tasks (tenant_id, parent_task_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_messages_run
  ON agent_messages (tenant_id, run_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_tool_executions_task
  ON tool_executions (tenant_id, task_id, created_at)
  WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_approval_requests_task_pending
  ON approval_requests (tenant_id, task_id, status, created_at)
  WHERE task_id IS NOT NULL AND status = 'pending';

DROP TRIGGER IF EXISTS trg_agents_updated_at ON agents;
CREATE TRIGGER trg_agents_updated_at
  BEFORE UPDATE ON agents
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

DROP TRIGGER IF EXISTS trg_agent_tasks_updated_at ON agent_tasks;
CREATE TRIGGER trg_agent_tasks_updated_at
  BEFORE UPDATE ON agent_tasks
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE OR REPLACE FUNCTION app.prevent_agent_message_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'agent_messages is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_messages_append_only_update ON agent_messages;
CREATE TRIGGER trg_agent_messages_append_only_update
  BEFORE UPDATE ON agent_messages
  FOR EACH ROW EXECUTE FUNCTION app.prevent_agent_message_mutation();

DROP TRIGGER IF EXISTS trg_agent_messages_append_only_delete ON agent_messages;
CREATE TRIGGER trg_agent_messages_append_only_delete
  BEFORE DELETE ON agent_messages
  FOR EACH ROW EXECUTE FUNCTION app.prevent_agent_message_mutation();

CREATE OR REPLACE FUNCTION app.validate_agent_task_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.status = NEW.status THEN
    RETURN NEW;
  END IF;

  IF NEW.status IN ('failed', 'blocked') THEN
    RETURN NEW;
  END IF;

  IF OLD.status = 'queued' AND NEW.status = 'thinking' THEN
    RETURN NEW;
  END IF;

  IF OLD.status = 'thinking' AND NEW.status IN (
    'needs_tool', 'waiting_approval', 'waiting_child', 'completed', 'failed'
  ) THEN
    RETURN NEW;
  END IF;

  IF OLD.status = 'needs_tool' AND NEW.status = 'tool_running' THEN
    RETURN NEW;
  END IF;

  IF OLD.status = 'tool_running' AND NEW.status = 'queued' THEN
    RETURN NEW;
  END IF;

  IF OLD.status IN ('waiting_approval', 'waiting_child') AND NEW.status = 'queued' THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'invalid agent_task status transition: % -> %', OLD.status, NEW.status;
END;
$$;

CREATE OR REPLACE FUNCTION app.log_agent_task_status_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.status <> NEW.status THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (
      NEW.tenant_id,
      NEW.run_id,
      'task_status_changed',
      jsonb_build_object(
        'task_id', NEW.id,
        'agent_id', NEW.agent_id,
        'from', OLD.status,
        'to', NEW.status
      ),
      'db'
    );
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_tasks_validate_transition ON agent_tasks;
CREATE TRIGGER trg_agent_tasks_validate_transition
  BEFORE UPDATE OF status ON agent_tasks
  FOR EACH ROW EXECUTE FUNCTION app.validate_agent_task_transition();

DROP TRIGGER IF EXISTS trg_agent_tasks_log_status_change ON agent_tasks;
CREATE TRIGGER trg_agent_tasks_log_status_change
  AFTER UPDATE OF status ON agent_tasks
  FOR EACH ROW EXECUTE FUNCTION app.log_agent_task_status_change();

ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE agents FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_tasks FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_messages FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agents_tenant_isolation ON agents;
CREATE POLICY agents_tenant_isolation ON agents
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS agent_tasks_tenant_isolation ON agent_tasks;
CREATE POLICY agent_tasks_tenant_isolation ON agent_tasks
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS agent_messages_tenant_isolation ON agent_messages;
CREATE POLICY agent_messages_tenant_isolation ON agent_messages
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());
