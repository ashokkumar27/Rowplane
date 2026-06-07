-- Govern model calls before workers invoke external LLM clients.

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
      WHERE e.tenant_id = p_tenant_id
        AND e.event_type = 'model_call_reserved';
    ELSIF p_scope_type = 'run' THEN
      SELECT count(*)::integer INTO usage_count
      FROM agent_events e
      WHERE e.tenant_id = p_tenant_id
        AND e.run_id::text = p_scope_id
        AND e.event_type = 'model_call_reserved';
    ELSIF p_scope_type = 'task' THEN
      SELECT count(*)::integer INTO usage_count
      FROM agent_events e
      WHERE e.tenant_id = p_tenant_id
        AND e.event_type = 'model_call_reserved'
        AND e.payload->>'task_id' = p_scope_id;
    ELSIF p_scope_type = 'agent' THEN
      SELECT count(*)::integer INTO usage_count
      FROM agent_events e
      WHERE e.tenant_id = p_tenant_id
        AND e.event_type = 'model_call_reserved'
        AND e.payload->>'agent_id' = p_scope_id;
    END IF;
  END IF;

  RETURN COALESCE(usage_count, 0);
END;
$$;

CREATE OR REPLACE FUNCTION app.reserve_model_call(
  p_tenant_id uuid,
  p_run_id uuid,
  p_task_id uuid DEFAULT NULL,
  p_agent_id uuid DEFAULT NULL,
  p_model text DEFAULT 'unset',
  p_actor text DEFAULT 'worker'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  budget jsonb;
BEGIN
  budget := app.runtime_budget_allows(
    p_tenant_id,
    'model_calls',
    1,
    p_run_id,
    p_task_id,
    p_agent_id,
    p_actor
  );

  IF COALESCE((budget->>'allowed')::boolean, true) = false THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (
      p_tenant_id,
      p_run_id,
      'model_call_denied_by_budget',
      jsonb_build_object(
        'task_id', p_task_id,
        'agent_id', p_agent_id,
        'model', p_model,
        'budget', budget
      ),
      p_actor
    );
    RETURN jsonb_build_object(
      'decision', 'denied',
      'status', 'blocked',
      'reason', 'model_call_budget_exceeded',
      'budget', budget
    );
  END IF;

  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (
    p_tenant_id,
    p_run_id,
    'model_call_reserved',
    jsonb_build_object(
      'task_id', p_task_id,
      'agent_id', p_agent_id,
      'model', p_model,
      'budget', budget
    ),
    p_actor
  );

  RETURN jsonb_build_object(
    'decision', 'allowed',
    'status', 'reserved',
    'model', p_model,
    'task_id', p_task_id,
    'agent_id', p_agent_id,
    'budget', budget
  );
END;
$$;
