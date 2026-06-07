-- SQL-native scheduler kernel.
-- Workers claim durable work leases from Postgres; external I/O remains outside
-- the database. This is the foundation for PgBouncer-friendly stateless workers,
-- fan-out/fan-in, and policy-driven concurrency.

CREATE TABLE IF NOT EXISTS agent_runtime_limits (
  tenant_id uuid PRIMARY KEY,
  max_concurrent_work integer CHECK (max_concurrent_work IS NULL OR max_concurrent_work > 0),
  max_concurrent_runs integer CHECK (max_concurrent_runs IS NULL OR max_concurrent_runs > 0),
  max_concurrent_tasks integer CHECK (max_concurrent_tasks IS NULL OR max_concurrent_tasks > 0),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_work_leases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  task_id uuid,
  work_type text NOT NULL CHECK (work_type IN ('run', 'task')),
  worker_id text NOT NULL CHECK (length(worker_id) > 0),
  capabilities text[] NOT NULL DEFAULT ARRAY[]::text[],
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'released', 'expired', 'failed')),
  lease_expires_at timestamptz NOT NULL,
  claimed_at timestamptz NOT NULL DEFAULT now(),
  heartbeat_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, task_id) REFERENCES agent_tasks (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_work_leases_tenant_status
  ON agent_work_leases (tenant_id, status, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_agent_work_leases_run
  ON agent_work_leases (tenant_id, run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_work_leases_task
  ON agent_work_leases (tenant_id, task_id, created_at DESC)
  WHERE task_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_work_leases_one_active_run
  ON agent_work_leases (tenant_id, run_id)
  WHERE status = 'active' AND task_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_work_leases_one_active_task
  ON agent_work_leases (tenant_id, task_id)
  WHERE status = 'active' AND task_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_agent_runtime_limits_updated_at ON agent_runtime_limits;
CREATE TRIGGER trg_agent_runtime_limits_updated_at
  BEFORE UPDATE ON agent_runtime_limits
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

DROP TRIGGER IF EXISTS trg_agent_work_leases_updated_at ON agent_work_leases;
CREATE TRIGGER trg_agent_work_leases_updated_at
  BEFORE UPDATE ON agent_work_leases
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE agent_runtime_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_work_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime_limits FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_work_leases FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_runtime_limits_tenant_isolation ON agent_runtime_limits;
CREATE POLICY agent_runtime_limits_tenant_isolation ON agent_runtime_limits
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

DROP POLICY IF EXISTS agent_work_leases_tenant_isolation ON agent_work_leases;
CREATE POLICY agent_work_leases_tenant_isolation ON agent_work_leases
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

CREATE OR REPLACE FUNCTION app.expire_agent_work_leases(p_tenant_id uuid, p_actor text DEFAULT 'scheduler')
RETURNS integer
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  expired_count integer;
BEGIN
  WITH expired AS (
    UPDATE agent_work_leases
    SET status = 'expired',
        completed_at = COALESCE(completed_at, now())
    WHERE tenant_id = p_tenant_id
      AND status = 'active'
      AND lease_expires_at <= now()
    RETURNING tenant_id, run_id, task_id, id, worker_id, work_type
  ),
  events AS (
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    SELECT
      ex.tenant_id,
      ex.run_id,
      'work_lease_expired',
      jsonb_build_object(
        'work_lease_id', ex.id,
        'task_id', ex.task_id,
        'work_type', ex.work_type,
        'worker_id', ex.worker_id
      ),
      p_actor
    FROM expired ex
    RETURNING 1
  )
  SELECT count(*) INTO expired_count FROM events;

  RETURN COALESCE(expired_count, 0);
END;
$$;

CREATE OR REPLACE FUNCTION app.claim_agent_work(
  p_tenant_id uuid,
  p_worker_id text,
  p_capabilities text[] DEFAULT ARRAY[]::text[],
  p_max_items integer DEFAULT 1,
  p_lease_seconds integer DEFAULT 60,
  p_kinds text[] DEFAULT ARRAY['task','run']::text[],
  p_actor text DEFAULT 'scheduler'
)
RETURNS TABLE(
  work_lease_id uuid,
  tenant_id uuid,
  run_id uuid,
  task_id uuid,
  work_type text,
  lease_expires_at timestamptz,
  payload jsonb
)
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  v_max_items integer;
  v_lease_seconds integer;
  v_active_total integer;
  v_active_runs integer;
  v_active_tasks integer;
  v_total_remaining integer;
  v_run_remaining integer;
  v_task_remaining integer;
  limits_row agent_runtime_limits%ROWTYPE;
BEGIN
  IF p_tenant_id IS NULL THEN
    RAISE EXCEPTION 'tenant_id is required' USING ERRCODE = '22023';
  END IF;
  IF length(btrim(COALESCE(p_worker_id, ''))) = 0 THEN
    RAISE EXCEPTION 'worker_id is required' USING ERRCODE = '22023';
  END IF;

  v_max_items := LEAST(GREATEST(COALESCE(p_max_items, 1), 1), 100);
  v_lease_seconds := LEAST(GREATEST(COALESCE(p_lease_seconds, 60), 5), 3600);

  PERFORM app.expire_agent_work_leases(p_tenant_id, p_actor);

  SELECT * INTO limits_row
  FROM agent_runtime_limits arl
  WHERE arl.tenant_id = p_tenant_id
  FOR UPDATE;

  SELECT
    count(*)::integer,
    count(*) FILTER (WHERE awl.work_type = 'run')::integer,
    count(*) FILTER (WHERE awl.work_type = 'task')::integer
  INTO v_active_total, v_active_runs, v_active_tasks
  FROM agent_work_leases awl
  WHERE awl.tenant_id = p_tenant_id
    AND awl.status = 'active'
    AND awl.lease_expires_at > now();

  v_total_remaining := CASE
    WHEN limits_row.max_concurrent_work IS NULL THEN v_max_items
    ELSE GREATEST(limits_row.max_concurrent_work - COALESCE(v_active_total, 0), 0)
  END;
  v_run_remaining := CASE
    WHEN limits_row.max_concurrent_runs IS NULL THEN v_max_items
    ELSE GREATEST(limits_row.max_concurrent_runs - COALESCE(v_active_runs, 0), 0)
  END;
  v_task_remaining := CASE
    WHEN limits_row.max_concurrent_tasks IS NULL THEN v_max_items
    ELSE GREATEST(limits_row.max_concurrent_tasks - COALESCE(v_active_tasks, 0), 0)
  END;

  IF v_total_remaining <= 0 THEN
    RETURN;
  END IF;

  RETURN QUERY
  WITH task_candidates AS (
    SELECT
      t.tenant_id,
      t.run_id,
      t.id AS task_id,
      'task'::text AS work_type,
      t.created_at,
      jsonb_build_object(
        'task', to_jsonb(t),
        'run_status', r.status,
        'agent_id', t.agent_id
      ) AS payload
    FROM agent_tasks t
    JOIN agent_runs r ON r.tenant_id = t.tenant_id AND r.id = t.run_id
    WHERE t.tenant_id = p_tenant_id
      AND 'task' = ANY(p_kinds)
      AND t.status = 'queued'
      AND r.status NOT IN ('completed', 'failed', 'blocked')
      AND NOT EXISTS (
        SELECT 1
        FROM agent_work_leases l
        WHERE l.tenant_id = t.tenant_id
          AND l.task_id = t.id
          AND l.status = 'active'
          AND l.lease_expires_at > now()
      )
    ORDER BY t.created_at, t.id
    LIMIT LEAST(v_max_items, v_total_remaining, v_task_remaining)
    FOR UPDATE OF t SKIP LOCKED
  ),
  task_leases AS (
    INSERT INTO agent_work_leases (
      tenant_id, run_id, task_id, work_type, worker_id, capabilities, lease_expires_at, metadata
    )
    SELECT
      tc.tenant_id,
      tc.run_id,
      tc.task_id,
      tc.work_type,
      p_worker_id,
      COALESCE(p_capabilities, ARRAY[]::text[]),
      now() + make_interval(secs => v_lease_seconds),
      jsonb_build_object('claim_source', 'app.claim_agent_work')
    FROM task_candidates tc
    ON CONFLICT DO NOTHING
    RETURNING agent_work_leases.id, agent_work_leases.tenant_id, agent_work_leases.run_id, agent_work_leases.task_id, agent_work_leases.work_type, agent_work_leases.lease_expires_at
  ),
  run_candidates AS (
    SELECT
      r.tenant_id,
      r.id AS run_id,
      NULL::uuid AS task_id,
      'run'::text AS work_type,
      r.created_at,
      jsonb_build_object('run', to_jsonb(r)) AS payload
    FROM agent_runs r
    WHERE r.tenant_id = p_tenant_id
      AND 'run' = ANY(p_kinds)
      AND r.status = 'queued'
      AND NOT EXISTS (
        SELECT 1
        FROM agent_tasks queued_task
        WHERE queued_task.tenant_id = r.tenant_id
          AND queued_task.run_id = r.id
          AND queued_task.status = 'queued'
      )
      AND NOT EXISTS (
        SELECT 1
        FROM agent_work_leases l
        WHERE l.tenant_id = r.tenant_id
          AND l.run_id = r.id
          AND l.task_id IS NULL
          AND l.status = 'active'
          AND l.lease_expires_at > now()
      )
    ORDER BY r.created_at, r.id
    LIMIT LEAST(
      v_max_items,
      GREATEST(v_total_remaining - (SELECT count(*)::integer FROM task_leases), 0),
      v_run_remaining
    )
    FOR UPDATE OF r SKIP LOCKED
  ),
  run_leases AS (
    INSERT INTO agent_work_leases (
      tenant_id, run_id, task_id, work_type, worker_id, capabilities, lease_expires_at, metadata
    )
    SELECT
      rc.tenant_id,
      rc.run_id,
      rc.task_id,
      rc.work_type,
      p_worker_id,
      COALESCE(p_capabilities, ARRAY[]::text[]),
      now() + make_interval(secs => v_lease_seconds),
      jsonb_build_object('claim_source', 'app.claim_agent_work')
    FROM run_candidates rc
    ON CONFLICT DO NOTHING
    RETURNING agent_work_leases.id, agent_work_leases.tenant_id, agent_work_leases.run_id, agent_work_leases.task_id, agent_work_leases.work_type, agent_work_leases.lease_expires_at
  ),
  all_leases AS (
    SELECT
      l.id,
      l.tenant_id,
      l.run_id,
      l.task_id,
      l.work_type,
      l.lease_expires_at,
      c.payload
    FROM task_leases l
    JOIN task_candidates c ON c.task_id = l.task_id
    UNION ALL
    SELECT
      l.id,
      l.tenant_id,
      l.run_id,
      l.task_id,
      l.work_type,
      l.lease_expires_at,
      c.payload
    FROM run_leases l
    JOIN run_candidates c ON c.run_id = l.run_id
  ),
  events AS (
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    SELECT
      al.tenant_id,
      al.run_id,
      'work_claimed',
      jsonb_build_object(
        'work_lease_id', al.id,
        'task_id', al.task_id,
        'work_type', al.work_type,
        'worker_id', p_worker_id,
        'lease_expires_at', al.lease_expires_at
      ),
      p_actor
    FROM all_leases al
    RETURNING 1
  )
  SELECT
    al.id,
    al.tenant_id,
    al.run_id,
    al.task_id,
    al.work_type,
    al.lease_expires_at,
    al.payload
  FROM all_leases al
  ORDER BY al.work_type DESC, al.lease_expires_at, al.id;
END;
$$;

CREATE OR REPLACE FUNCTION app.heartbeat_agent_work(
  p_work_lease_id uuid,
  p_worker_id text,
  p_lease_seconds integer DEFAULT 60,
  p_actor text DEFAULT 'scheduler'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  lease_row agent_work_leases%ROWTYPE;
  v_lease_seconds integer;
BEGIN
  v_lease_seconds := LEAST(GREATEST(COALESCE(p_lease_seconds, 60), 5), 3600);

  UPDATE agent_work_leases
  SET heartbeat_at = now(),
      lease_expires_at = now() + make_interval(secs => v_lease_seconds)
  WHERE id = p_work_lease_id
    AND worker_id = p_worker_id
    AND status = 'active'
    AND lease_expires_at > now()
  RETURNING * INTO lease_row;

  IF NOT FOUND THEN
    RETURN jsonb_build_object('decision', 'not_active', 'status', 'failed');
  END IF;

  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (
    lease_row.tenant_id,
    lease_row.run_id,
    'work_heartbeat',
    jsonb_build_object(
      'work_lease_id', lease_row.id,
      'task_id', lease_row.task_id,
      'work_type', lease_row.work_type,
      'worker_id', lease_row.worker_id,
      'lease_expires_at', lease_row.lease_expires_at
    ),
    p_actor
  );

  RETURN jsonb_build_object(
    'decision', 'extended',
    'status', 'active',
    'work_lease_id', lease_row.id,
    'lease_expires_at', lease_row.lease_expires_at
  );
END;
$$;

CREATE OR REPLACE FUNCTION app.complete_agent_work(
  p_work_lease_id uuid,
  p_worker_id text,
  p_status text DEFAULT 'completed',
  p_metadata jsonb DEFAULT '{}'::jsonb,
  p_actor text DEFAULT 'scheduler'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  lease_row agent_work_leases%ROWTYPE;
  v_status text;
BEGIN
  v_status := COALESCE(p_status, 'completed');
  IF v_status NOT IN ('completed', 'released', 'failed') THEN
    RAISE EXCEPTION 'work completion status must be completed, released, or failed' USING ERRCODE = '22023';
  END IF;

  UPDATE agent_work_leases
  SET status = v_status,
      completed_at = now(),
      metadata = metadata || COALESCE(p_metadata, '{}'::jsonb)
  WHERE id = p_work_lease_id
    AND worker_id = p_worker_id
    AND status = 'active'
  RETURNING * INTO lease_row;

  IF NOT FOUND THEN
    RETURN jsonb_build_object('decision', 'not_active', 'status', 'failed');
  END IF;

  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (
    lease_row.tenant_id,
    lease_row.run_id,
    'work_lease_completed',
    jsonb_build_object(
      'work_lease_id', lease_row.id,
      'task_id', lease_row.task_id,
      'work_type', lease_row.work_type,
      'worker_id', lease_row.worker_id,
      'status', lease_row.status,
      'metadata', p_metadata
    ),
    p_actor
  );

  RETURN jsonb_build_object(
    'decision', 'completed',
    'status', lease_row.status,
    'work_lease_id', lease_row.id
  );
END;
$$;
