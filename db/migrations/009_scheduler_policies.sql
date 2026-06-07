-- Scheduler policies: capabilities, priority, not-before, and deadlines.

ALTER TABLE agent_runs
  ADD COLUMN IF NOT EXISTS required_capabilities text[] NOT NULL DEFAULT ARRAY[]::text[],
  ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS not_before timestamptz,
  ADD COLUMN IF NOT EXISTS deadline_at timestamptz;

ALTER TABLE agent_tasks
  ADD COLUMN IF NOT EXISTS required_capabilities text[] NOT NULL DEFAULT ARRAY[]::text[],
  ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS not_before timestamptz,
  ADD COLUMN IF NOT EXISTS deadline_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_agent_runs_scheduler_ready
  ON agent_runs (tenant_id, status, priority DESC, deadline_at ASC NULLS LAST, created_at ASC)
  WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_agent_tasks_scheduler_ready
  ON agent_tasks (tenant_id, status, priority DESC, deadline_at ASC NULLS LAST, created_at ASC)
  WHERE status = 'queued';

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
      t.priority,
      t.deadline_at,
      jsonb_build_object(
        'task', to_jsonb(t),
        'run_status', r.status,
        'agent_id', t.agent_id,
        'required_capabilities', t.required_capabilities,
        'priority', t.priority,
        'deadline_at', t.deadline_at
      ) AS payload
    FROM agent_tasks t
    JOIN agent_runs r ON r.tenant_id = t.tenant_id AND r.id = t.run_id
    WHERE t.tenant_id = p_tenant_id
      AND 'task' = ANY(p_kinds)
      AND t.status = 'queued'
      AND r.status NOT IN ('completed', 'failed', 'blocked')
      AND (t.not_before IS NULL OR t.not_before <= now())
      AND COALESCE(t.required_capabilities, ARRAY[]::text[]) <@ COALESCE(p_capabilities, ARRAY[]::text[])
      AND NOT EXISTS (
        SELECT 1
        FROM agent_work_leases l
        WHERE l.tenant_id = t.tenant_id
          AND l.task_id = t.id
          AND l.status = 'active'
          AND l.lease_expires_at > now()
      )
    ORDER BY t.priority DESC, t.deadline_at ASC NULLS LAST, t.created_at ASC, t.id ASC
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
      r.priority,
      r.deadline_at,
      jsonb_build_object(
        'run', to_jsonb(r),
        'required_capabilities', r.required_capabilities,
        'priority', r.priority,
        'deadline_at', r.deadline_at
      ) AS payload
    FROM agent_runs r
    WHERE r.tenant_id = p_tenant_id
      AND 'run' = ANY(p_kinds)
      AND r.status = 'queued'
      AND (r.not_before IS NULL OR r.not_before <= now())
      AND COALESCE(r.required_capabilities, ARRAY[]::text[]) <@ COALESCE(p_capabilities, ARRAY[]::text[])
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
    ORDER BY r.priority DESC, r.deadline_at ASC NULLS LAST, r.created_at ASC, r.id ASC
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
      c.payload,
      c.priority,
      c.deadline_at,
      c.created_at
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
      c.payload,
      c.priority,
      c.deadline_at,
      c.created_at
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
        'lease_expires_at', al.lease_expires_at,
        'capabilities', COALESCE(p_capabilities, ARRAY[]::text[]),
        'priority', al.priority,
        'deadline_at', al.deadline_at
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
  ORDER BY al.work_type DESC, al.priority DESC, al.deadline_at ASC NULLS LAST, al.created_at ASC, al.id;
END;
$$;
