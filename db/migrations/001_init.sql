CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgmq;
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS app;

CREATE OR REPLACE FUNCTION app.current_tenant_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
$$;

DO $$
BEGIN
  CREATE TYPE agent_run_status AS ENUM (
    'queued',
    'thinking',
    'needs_tool',
    'tool_running',
    'waiting_approval',
    'evaluating',
    'completed',
    'failed',
    'blocked'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
  CREATE TYPE tool_execution_status AS ENUM (
    'pending',
    'running',
    'waiting_approval',
    'completed',
    'failed'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
  CREATE TYPE approval_status AS ENUM (
    'pending',
    'approved',
    'rejected',
    'canceled'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS eval_cases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  name text NOT NULL,
  input jsonb NOT NULL DEFAULT '{}'::jsonb,
  expected jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  status agent_run_status NOT NULL DEFAULT 'queued',
  task jsonb NOT NULL DEFAULT '{}'::jsonb,
  answer jsonb,
  error text,
  iteration_count integer NOT NULL DEFAULT 0 CHECK (iteration_count >= 0),
  max_iterations integer NOT NULL DEFAULT 20 CHECK (max_iterations > 0),
  model text NOT NULL DEFAULT 'unset',
  eval_case_id uuid,
  locked_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, eval_case_id) REFERENCES eval_cases (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS agent_events (
  event_id bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  event_type text NOT NULL CHECK (length(event_type) > 0),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  actor text NOT NULL DEFAULT 'worker',
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_tools (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  name text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]*$'),
  description text NOT NULL DEFAULT '',
  input_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
  is_side_effecting boolean NOT NULL DEFAULT false,
  requires_approval boolean NOT NULL DEFAULT false,
  approval_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS agent_tool_permissions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  tool_id uuid NOT NULL,
  subject_type text NOT NULL CHECK (subject_type IN ('tenant', 'agent', 'user', 'run')),
  subject_id text NOT NULL,
  allowed boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, tool_id, subject_type, subject_id),
  FOREIGN KEY (tenant_id, tool_id) REFERENCES agent_tools (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tool_executions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  tool_id uuid NOT NULL,
  idempotency_key text NOT NULL,
  status tool_execution_status NOT NULL DEFAULT 'pending',
  arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
  arguments_hash text NOT NULL,
  result jsonb,
  error text,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, tool_id, idempotency_key),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, tool_id) REFERENCES agent_tools (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS approval_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  tool_execution_id uuid,
  status approval_status NOT NULL DEFAULT 'pending',
  reason text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  requested_by text NOT NULL DEFAULT 'worker',
  resolved_by text,
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, tool_execution_id) REFERENCES tool_executions (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_memory (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  memory_type text NOT NULL CHECK (length(memory_type) > 0),
  content text NOT NULL CHECK (length(content) > 0),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  embedding vector(1536),
  source_run_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, source_run_id) REFERENCES agent_runs (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS eval_results (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  eval_case_id uuid NOT NULL,
  run_id uuid NOT NULL,
  correctness numeric,
  tool_correctness numeric,
  retrieval_relevance numeric,
  format_compliance numeric,
  latency_ms integer,
  cost_usd numeric,
  human_agreement numeric,
  policy_compliance numeric,
  scores jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, eval_case_id, run_id),
  FOREIGN KEY (tenant_id, eval_case_id) REFERENCES eval_cases (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_status
  ON agent_runs (tenant_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agent_events_run
  ON agent_events (tenant_id, run_id, event_id);
CREATE INDEX IF NOT EXISTS idx_agent_tools_tenant_name
  ON agent_tools (tenant_id, name);
CREATE INDEX IF NOT EXISTS idx_tool_executions_run
  ON tool_executions (tenant_id, run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_approval_requests_pending
  ON approval_requests (tenant_id, status, created_at)
  WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_requests_one_per_tool_execution
  ON approval_requests (tenant_id, tool_execution_id)
  WHERE tool_execution_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_memory_tenant_type
  ON agent_memory (tenant_id, memory_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_memory_metadata
  ON agent_memory USING gin (metadata);
CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding
  ON agent_memory USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100)
  WHERE embedding IS NOT NULL;

CREATE OR REPLACE FUNCTION app.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION app.prevent_agent_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'agent_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_events_append_only_update ON agent_events;
CREATE TRIGGER trg_agent_events_append_only_update
  BEFORE UPDATE ON agent_events
  FOR EACH ROW EXECUTE FUNCTION app.prevent_agent_event_mutation();

DROP TRIGGER IF EXISTS trg_agent_events_append_only_delete ON agent_events;
CREATE TRIGGER trg_agent_events_append_only_delete
  BEFORE DELETE ON agent_events
  FOR EACH ROW EXECUTE FUNCTION app.prevent_agent_event_mutation();

CREATE OR REPLACE FUNCTION app.validate_agent_run_transition()
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

  IF OLD.status = 'thinking' AND NEW.status IN ('needs_tool', 'waiting_approval', 'completed', 'failed') THEN
    RETURN NEW;
  END IF;

  IF OLD.status = 'needs_tool' AND NEW.status = 'tool_running' THEN
    RETURN NEW;
  END IF;

  IF OLD.status = 'tool_running' AND NEW.status = 'queued' THEN
    RETURN NEW;
  END IF;

  IF OLD.status = 'waiting_approval' AND NEW.status = 'queued' THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'invalid agent_run status transition: % -> %', OLD.status, NEW.status;
END;
$$;

CREATE OR REPLACE FUNCTION app.log_agent_run_status_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.status <> NEW.status THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (
      NEW.tenant_id,
      NEW.id,
      'run_status_changed',
      jsonb_build_object('from', OLD.status, 'to', NEW.status),
      'db'
    );
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_runs_validate_transition ON agent_runs;
CREATE TRIGGER trg_agent_runs_validate_transition
  BEFORE UPDATE OF status ON agent_runs
  FOR EACH ROW EXECUTE FUNCTION app.validate_agent_run_transition();

DROP TRIGGER IF EXISTS trg_agent_runs_log_status_change ON agent_runs;
CREATE TRIGGER trg_agent_runs_log_status_change
  AFTER UPDATE OF status ON agent_runs
  FOR EACH ROW EXECUTE FUNCTION app.log_agent_run_status_change();

DROP TRIGGER IF EXISTS trg_agent_runs_updated_at ON agent_runs;
CREATE TRIGGER trg_agent_runs_updated_at
  BEFORE UPDATE ON agent_runs
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

DROP TRIGGER IF EXISTS trg_agent_tools_updated_at ON agent_tools;
CREATE TRIGGER trg_agent_tools_updated_at
  BEFORE UPDATE ON agent_tools
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

DROP TRIGGER IF EXISTS trg_tool_executions_updated_at ON tool_executions;
CREATE TRIGGER trg_tool_executions_updated_at
  BEFORE UPDATE ON tool_executions
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

DROP TRIGGER IF EXISTS trg_approval_requests_updated_at ON approval_requests;
CREATE TRIGGER trg_approval_requests_updated_at
  BEFORE UPDATE ON approval_requests
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

DROP TRIGGER IF EXISTS trg_eval_cases_updated_at ON eval_cases;
CREATE TRIGGER trg_eval_cases_updated_at
  BEFORE UPDATE ON eval_cases
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE eval_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_tools ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_tool_permissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE eval_results ENABLE ROW LEVEL SECURITY;

ALTER TABLE eval_cases FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_events FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_tools FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_tool_permissions FORCE ROW LEVEL SECURITY;
ALTER TABLE tool_executions FORCE ROW LEVEL SECURITY;
ALTER TABLE approval_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_memory FORCE ROW LEVEL SECURITY;
ALTER TABLE eval_results FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS eval_cases_tenant_isolation ON eval_cases;
CREATE POLICY eval_cases_tenant_isolation ON eval_cases
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS agent_runs_tenant_isolation ON agent_runs;
CREATE POLICY agent_runs_tenant_isolation ON agent_runs
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS agent_events_tenant_isolation ON agent_events;
CREATE POLICY agent_events_tenant_isolation ON agent_events
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS agent_tools_tenant_isolation ON agent_tools;
CREATE POLICY agent_tools_tenant_isolation ON agent_tools
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS agent_tool_permissions_tenant_isolation ON agent_tool_permissions;
CREATE POLICY agent_tool_permissions_tenant_isolation ON agent_tool_permissions
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS tool_executions_tenant_isolation ON tool_executions;
CREATE POLICY tool_executions_tenant_isolation ON tool_executions
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS approval_requests_tenant_isolation ON approval_requests;
CREATE POLICY approval_requests_tenant_isolation ON approval_requests
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS agent_memory_tenant_isolation ON agent_memory;
CREATE POLICY agent_memory_tenant_isolation ON agent_memory
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS eval_results_tenant_isolation ON eval_results;
CREATE POLICY eval_results_tenant_isolation ON eval_results
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DO $$
BEGIN
  PERFORM pgmq.create('agent_wakeups');
EXCEPTION
  WHEN duplicate_table OR unique_violation THEN NULL;
  WHEN undefined_function THEN
    RAISE NOTICE 'pgmq.create is unavailable; create queue agent_wakeups after installing pgmq';
END $$;
