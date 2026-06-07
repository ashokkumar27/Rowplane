-- Runtime budgets and quota checks.
-- Budgets are scoped policy rows; usage is derived from the durable runtime ledger.

CREATE TABLE IF NOT EXISTS agent_runtime_budgets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  scope_type text NOT NULL CHECK (scope_type IN ('tenant', 'run', 'task', 'agent')),
  scope_id text NOT NULL,
  max_model_calls integer CHECK (max_model_calls IS NULL OR max_model_calls >= 0),
  max_tool_executions integer CHECK (max_tool_executions IS NULL OR max_tool_executions >= 0),
  max_child_tasks integer CHECK (max_child_tasks IS NULL OR max_child_tasks >= 0),
  max_active_work integer CHECK (max_active_work IS NULL OR max_active_work >= 0),
  max_estimated_cost_usd numeric CHECK (max_estimated_cost_usd IS NULL OR max_estimated_cost_usd >= 0),
  period_start timestamptz,
  period_end timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, scope_type, scope_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_runtime_budgets_scope
  ON agent_runtime_budgets (tenant_id, scope_type, scope_id)
  WHERE enabled = true;

DROP TRIGGER IF EXISTS trg_agent_runtime_budgets_updated_at ON agent_runtime_budgets;
CREATE TRIGGER trg_agent_runtime_budgets_updated_at
  BEFORE UPDATE ON agent_runtime_budgets
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE agent_runtime_budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime_budgets FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_runtime_budgets_tenant_isolation ON agent_runtime_budgets;
CREATE POLICY agent_runtime_budgets_tenant_isolation ON agent_runtime_budgets
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

CREATE OR REPLACE FUNCTION app.runtime_budget_scope_usage(
  p_tenant_id uuid,
  p_scope_type text,
  p_scope_id text,
  p_metric text
)
RETURNS integer
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  usage_count integer := 0;
BEGIN
  IF p_metric = 'active_work' THEN
    IF p_scope_type = 'tenant' THEN
      SELECT count(*)::integer INTO usage_count
      FROM agent_work_leases l
      WHERE l.tenant_id = p_tenant_id
        AND l.status = 'active'
        AND l.lease_expires_at > now();
    ELSIF p_scope_type = 'run' THEN
      SELECT count(*)::integer INTO usage_count
      FROM agent_work_leases l
      WHERE l.tenant_id = p_tenant_id
        AND l.run_id::text = p_scope_id
        AND l.status = 'active'
        AND l.lease_expires_at > now();
    ELSIF p_scope_type = 'task' THEN
      SELECT count(*)::integer INTO usage_count
      FROM agent_work_leases l
      WHERE l.tenant_id = p_tenant_id
        AND l.task_id::text = p_scope_id
        AND l.status = 'active'
        AND l.lease_expires_at > now();
    ELSIF p_scope_type = 'agent' THEN
      SELECT count(*)::integer INTO usage_count
      FROM agent_work_leases l
      JOIN agent_tasks t ON t.tenant_id = l.tenant_id AND t.id = l.task_id
      WHERE l.tenant_id = p_tenant_id
        AND t.agent_id::text = p_scope_id
        AND l.status = 'active'
        AND l.lease_expires_at > now();
    END IF;
  ELSIF p_metric = 'child_tasks' THEN
    IF p_scope_type = 'tenant' THEN
      SELECT count(*)::integer INTO usage_count FROM agent_tasks t WHERE t.tenant_id = p_tenant_id;
    ELSIF p_scope_type = 'run' THEN
      SELECT count(*)::integer INTO usage_count FROM agent_tasks t WHERE t.tenant_id = p_tenant_id AND t.run_id::text = p_scope_id;
    ELSIF p_scope_type = 'task' THEN
      SELECT count(*)::integer INTO usage_count FROM agent_tasks t WHERE t.tenant_id = p_tenant_id AND t.parent_task_id::text = p_scope_id;
    ELSIF p_scope_type = 'agent' THEN
      SELECT count(*)::integer INTO usage_count FROM agent_tasks t WHERE t.tenant_id = p_tenant_id AND t.agent_id::text = p_scope_id;
    END IF;
  ELSIF p_metric = 'tool_executions' THEN
    IF p_scope_type = 'tenant' THEN
      SELECT count(*)::integer INTO usage_count FROM tool_executions te WHERE te.tenant_id = p_tenant_id;
    ELSIF p_scope_type = 'run' THEN
      SELECT count(*)::integer INTO usage_count FROM tool_executions te WHERE te.tenant_id = p_tenant_id AND te.run_id::text = p_scope_id;
    ELSIF p_scope_type = 'task' THEN
      SELECT count(*)::integer INTO usage_count FROM tool_executions te WHERE te.tenant_id = p_tenant_id AND te.task_id::text = p_scope_id;
    END IF;
  ELSIF p_metric = 'model_calls' THEN
    IF p_scope_type = 'tenant' THEN
      SELECT count(*)::integer INTO usage_count
      FROM agent_events e
      WHERE e.tenant_id = p_tenant_id AND e.event_type IN ('run_thinking', 'task_thinking');
    ELSIF p_scope_type = 'run' THEN
      SELECT count(*)::integer INTO usage_count
      FROM agent_events e
      WHERE e.tenant_id = p_tenant_id AND e.run_id::text = p_scope_id AND e.event_type IN ('run_thinking', 'task_thinking');
    ELSIF p_scope_type = 'task' THEN
      SELECT count(*)::integer INTO usage_count
      FROM agent_events e
      WHERE e.tenant_id = p_tenant_id
        AND e.event_type = 'task_thinking'
        AND e.payload->>'task_id' = p_scope_id;
    END IF;
  END IF;

  RETURN COALESCE(usage_count, 0);
END;
$$;

CREATE OR REPLACE FUNCTION app.runtime_budget_allows(
  p_tenant_id uuid,
  p_metric text,
  p_increment integer DEFAULT 1,
  p_run_id uuid DEFAULT NULL,
  p_task_id uuid DEFAULT NULL,
  p_agent_id uuid DEFAULT NULL,
  p_actor text DEFAULT 'scheduler'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  budget_row agent_runtime_budgets%ROWTYPE;
  scope_usage integer;
  cap_value integer;
  scopes jsonb;
  checked jsonb := '[]'::jsonb;
BEGIN
  IF p_metric NOT IN ('active_work', 'child_tasks', 'tool_executions', 'model_calls') THEN
    RAISE EXCEPTION 'unsupported budget metric: %', p_metric USING ERRCODE = '22023';
  END IF;

  scopes := jsonb_build_array(jsonb_build_object('scope_type', 'tenant', 'scope_id', p_tenant_id::text));
  IF p_run_id IS NOT NULL THEN
    scopes := scopes || jsonb_build_array(jsonb_build_object('scope_type', 'run', 'scope_id', p_run_id::text));
  END IF;
  IF p_task_id IS NOT NULL THEN
    scopes := scopes || jsonb_build_array(jsonb_build_object('scope_type', 'task', 'scope_id', p_task_id::text));
  END IF;
  IF p_agent_id IS NOT NULL THEN
    scopes := scopes || jsonb_build_array(jsonb_build_object('scope_type', 'agent', 'scope_id', p_agent_id::text));
  END IF;

  FOR budget_row IN
    SELECT b.*
    FROM agent_runtime_budgets b
    JOIN jsonb_to_recordset(scopes) AS s(scope_type text, scope_id text)
      ON s.scope_type = b.scope_type AND s.scope_id = b.scope_id
    WHERE b.tenant_id = p_tenant_id
      AND b.enabled = true
      AND (b.period_start IS NULL OR b.period_start <= now())
      AND (b.period_end IS NULL OR b.period_end > now())
    ORDER BY CASE b.scope_type WHEN 'task' THEN 0 WHEN 'run' THEN 1 WHEN 'agent' THEN 2 WHEN 'tenant' THEN 3 ELSE 4 END
  LOOP
    cap_value := CASE p_metric
      WHEN 'active_work' THEN budget_row.max_active_work
      WHEN 'child_tasks' THEN budget_row.max_child_tasks
      WHEN 'tool_executions' THEN budget_row.max_tool_executions
      WHEN 'model_calls' THEN budget_row.max_model_calls
    END;
    IF cap_value IS NULL THEN
      CONTINUE;
    END IF;

    scope_usage := app.runtime_budget_scope_usage(
      p_tenant_id,
      budget_row.scope_type,
      budget_row.scope_id,
      p_metric
    );
    checked := checked || jsonb_build_array(jsonb_build_object(
      'budget_id', budget_row.id,
      'scope_type', budget_row.scope_type,
      'scope_id', budget_row.scope_id,
      'metric', p_metric,
      'usage', scope_usage,
      'increment', COALESCE(p_increment, 1),
      'limit', cap_value
    ));

    IF scope_usage + COALESCE(p_increment, 1) > cap_value THEN
      IF p_run_id IS NOT NULL THEN
        INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
        VALUES (
          p_tenant_id,
          p_run_id,
          'runtime_budget_exceeded',
          jsonb_build_object(
            'budget_id', budget_row.id,
            'scope_type', budget_row.scope_type,
            'scope_id', budget_row.scope_id,
            'metric', p_metric,
            'usage', scope_usage,
            'increment', COALESCE(p_increment, 1),
            'limit', cap_value,
            'task_id', p_task_id,
            'agent_id', p_agent_id
          ),
          p_actor
        );
      END IF;
      RETURN jsonb_build_object(
        'allowed', false,
        'decision', 'denied',
        'metric', p_metric,
        'budget_id', budget_row.id,
        'scope_type', budget_row.scope_type,
        'scope_id', budget_row.scope_id,
        'usage', scope_usage,
        'increment', COALESCE(p_increment, 1),
        'limit', cap_value,
        'checked', checked
      );
    END IF;
  END LOOP;

  IF p_run_id IS NOT NULL AND jsonb_array_length(checked) > 0 THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (
      p_tenant_id,
      p_run_id,
      'runtime_budget_checked',
      jsonb_build_object('metric', p_metric, 'allowed', true, 'checked', checked, 'task_id', p_task_id, 'agent_id', p_agent_id),
      p_actor
    );
  END IF;

  RETURN jsonb_build_object('allowed', true, 'decision', 'allowed', 'metric', p_metric, 'checked', checked);
END;
$$;

CREATE OR REPLACE FUNCTION app.runtime_active_work_budget_allows(
  p_tenant_id uuid,
  p_run_id uuid,
  p_task_id uuid DEFAULT NULL
)
RETURNS boolean
LANGUAGE sql
VOLATILE
AS $$
  SELECT COALESCE((app.runtime_budget_allows(p_tenant_id, 'active_work', 1, p_run_id, p_task_id, NULL, 'scheduler')->>'allowed')::boolean, true);
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
  tenant_budget jsonb;
  v_budget_total_remaining integer;
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
  tenant_budget := app.runtime_budget_allows(p_tenant_id, 'active_work', 1, NULL, NULL, NULL, p_actor);
  IF COALESCE((tenant_budget->>'allowed')::boolean, true) = false THEN
    RETURN;
  END IF;

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

  SELECT min(GREATEST(b.max_active_work - COALESCE(v_active_total, 0), 0))::integer
  INTO v_budget_total_remaining
  FROM agent_runtime_budgets b
  WHERE b.tenant_id = p_tenant_id
    AND b.scope_type = 'tenant'
    AND b.scope_id = p_tenant_id::text
    AND b.enabled = true
    AND b.max_active_work IS NOT NULL
    AND (b.period_start IS NULL OR b.period_start <= now())
    AND (b.period_end IS NULL OR b.period_end > now());

  IF v_budget_total_remaining IS NOT NULL THEN
    v_total_remaining := LEAST(v_total_remaining, v_budget_total_remaining);
  END IF;

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
      AND app.runtime_active_work_budget_allows(t.tenant_id, t.run_id, t.id)
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
      AND app.runtime_active_work_budget_allows(r.tenant_id, r.id, NULL)
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
