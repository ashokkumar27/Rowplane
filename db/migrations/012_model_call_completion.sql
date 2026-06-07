-- Complete the model-call ledger with latency, token, and cost accounting.

CREATE OR REPLACE FUNCTION app.runtime_cost_budget_scope_usage(
  p_tenant_id uuid,
  p_scope_type text,
  p_scope_id text
)
RETURNS numeric
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  usage_count numeric := 0;
BEGIN
  IF p_scope_type = 'tenant' THEN
    SELECT COALESCE(sum(COALESCE(NULLIF(e.payload->>'estimated_cost_usd', '')::numeric, 0)), 0) INTO usage_count
    FROM agent_events e
    WHERE e.tenant_id = p_tenant_id
      AND e.event_type IN ('model_call_completed', 'model_call_failed');
  ELSIF p_scope_type = 'run' THEN
    SELECT COALESCE(sum(COALESCE(NULLIF(e.payload->>'estimated_cost_usd', '')::numeric, 0)), 0) INTO usage_count
    FROM agent_events e
    WHERE e.tenant_id = p_tenant_id
      AND e.run_id::text = p_scope_id
      AND e.event_type IN ('model_call_completed', 'model_call_failed');
  ELSIF p_scope_type = 'task' THEN
    SELECT COALESCE(sum(COALESCE(NULLIF(e.payload->>'estimated_cost_usd', '')::numeric, 0)), 0) INTO usage_count
    FROM agent_events e
    WHERE e.tenant_id = p_tenant_id
      AND e.event_type IN ('model_call_completed', 'model_call_failed')
      AND e.payload->>'task_id' = p_scope_id;
  ELSIF p_scope_type = 'agent' THEN
    SELECT COALESCE(sum(COALESCE(NULLIF(e.payload->>'estimated_cost_usd', '')::numeric, 0)), 0) INTO usage_count
    FROM agent_events e
    WHERE e.tenant_id = p_tenant_id
      AND e.event_type IN ('model_call_completed', 'model_call_failed')
      AND e.payload->>'agent_id' = p_scope_id;
  END IF;

  RETURN COALESCE(usage_count, 0);
END;
$$;

CREATE OR REPLACE FUNCTION app.runtime_cost_budget_allows(
  p_tenant_id uuid,
  p_projected_cost_usd numeric DEFAULT 0,
  p_run_id uuid DEFAULT NULL,
  p_task_id uuid DEFAULT NULL,
  p_agent_id uuid DEFAULT NULL,
  p_actor text DEFAULT 'worker'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  budget_row agent_runtime_budgets%ROWTYPE;
  scope_usage numeric;
  projected numeric := COALESCE(p_projected_cost_usd, 0);
  scopes jsonb;
  checked jsonb := '[]'::jsonb;
BEGIN
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
      AND b.max_estimated_cost_usd IS NOT NULL
      AND (b.period_start IS NULL OR b.period_start <= now())
      AND (b.period_end IS NULL OR b.period_end > now())
    ORDER BY CASE b.scope_type WHEN 'task' THEN 0 WHEN 'run' THEN 1 WHEN 'agent' THEN 2 WHEN 'tenant' THEN 3 ELSE 4 END
  LOOP
    scope_usage := app.runtime_cost_budget_scope_usage(
      p_tenant_id,
      budget_row.scope_type,
      budget_row.scope_id
    );
    checked := checked || jsonb_build_array(jsonb_build_object(
      'budget_id', budget_row.id,
      'scope_type', budget_row.scope_type,
      'scope_id', budget_row.scope_id,
      'metric', 'estimated_cost_usd',
      'usage', scope_usage,
      'projected_cost_usd', projected,
      'limit', budget_row.max_estimated_cost_usd
    ));

    IF scope_usage + projected > budget_row.max_estimated_cost_usd THEN
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
            'metric', 'estimated_cost_usd',
            'usage', scope_usage,
            'projected_cost_usd', projected,
            'limit', budget_row.max_estimated_cost_usd,
            'task_id', p_task_id,
            'agent_id', p_agent_id
          ),
          p_actor
        );
      END IF;
      RETURN jsonb_build_object(
        'allowed', false,
        'decision', 'denied',
        'metric', 'estimated_cost_usd',
        'budget_id', budget_row.id,
        'scope_type', budget_row.scope_type,
        'scope_id', budget_row.scope_id,
        'usage', scope_usage,
        'projected_cost_usd', projected,
        'limit', budget_row.max_estimated_cost_usd,
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
      jsonb_build_object(
        'metric', 'estimated_cost_usd',
        'allowed', true,
        'checked', checked,
        'task_id', p_task_id,
        'agent_id', p_agent_id
      ),
      p_actor
    );
  END IF;

  RETURN jsonb_build_object('allowed', true, 'decision', 'allowed', 'metric', 'estimated_cost_usd', 'checked', checked);
END;
$$;

DROP FUNCTION IF EXISTS app.reserve_model_call(uuid, uuid, uuid, uuid, text, text);

CREATE OR REPLACE FUNCTION app.reserve_model_call(
  p_tenant_id uuid,
  p_run_id uuid,
  p_task_id uuid DEFAULT NULL,
  p_agent_id uuid DEFAULT NULL,
  p_model text DEFAULT 'unset',
  p_actor text DEFAULT 'worker',
  p_projected_cost_usd numeric DEFAULT 0
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  call_budget jsonb;
  cost_budget jsonb;
BEGIN
  call_budget := app.runtime_budget_allows(
    p_tenant_id,
    'model_calls',
    1,
    p_run_id,
    p_task_id,
    p_agent_id,
    p_actor
  );

  IF COALESCE((call_budget->>'allowed')::boolean, true) = false THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (
      p_tenant_id,
      p_run_id,
      'model_call_denied_by_budget',
      jsonb_build_object(
        'task_id', p_task_id,
        'agent_id', p_agent_id,
        'model', p_model,
        'budget', call_budget
      ),
      p_actor
    );
    RETURN jsonb_build_object(
      'decision', 'denied',
      'status', 'blocked',
      'reason', 'model_call_budget_exceeded',
      'budget', call_budget
    );
  END IF;

  cost_budget := app.runtime_cost_budget_allows(
    p_tenant_id,
    p_projected_cost_usd,
    p_run_id,
    p_task_id,
    p_agent_id,
    p_actor
  );

  IF COALESCE((cost_budget->>'allowed')::boolean, true) = false THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (
      p_tenant_id,
      p_run_id,
      'model_call_denied_by_budget',
      jsonb_build_object(
        'task_id', p_task_id,
        'agent_id', p_agent_id,
        'model', p_model,
        'budget', cost_budget
      ),
      p_actor
    );
    RETURN jsonb_build_object(
      'decision', 'denied',
      'status', 'blocked',
      'reason', 'model_cost_budget_exceeded',
      'budget', cost_budget
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
      'projected_cost_usd', COALESCE(p_projected_cost_usd, 0),
      'budget', call_budget,
      'cost_budget', cost_budget
    ),
    p_actor
  );

  RETURN jsonb_build_object(
    'decision', 'allowed',
    'status', 'reserved',
    'model', p_model,
    'task_id', p_task_id,
    'agent_id', p_agent_id,
    'projected_cost_usd', COALESCE(p_projected_cost_usd, 0),
    'budget', call_budget,
    'cost_budget', cost_budget
  );
END;
$$;

CREATE OR REPLACE FUNCTION app.complete_model_call(
  p_tenant_id uuid,
  p_run_id uuid,
  p_task_id uuid DEFAULT NULL,
  p_agent_id uuid DEFAULT NULL,
  p_model text DEFAULT 'unset',
  p_status text DEFAULT 'completed',
  p_latency_ms integer DEFAULT NULL,
  p_prompt_tokens integer DEFAULT NULL,
  p_completion_tokens integer DEFAULT NULL,
  p_total_tokens integer DEFAULT NULL,
  p_estimated_cost_usd numeric DEFAULT NULL,
  p_error text DEFAULT NULL,
  p_actor text DEFAULT 'worker'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  event_name text;
BEGIN
  IF p_status NOT IN ('completed', 'failed') THEN
    RAISE EXCEPTION 'unsupported model call status: %', p_status USING ERRCODE = '22023';
  END IF;

  event_name := CASE p_status WHEN 'failed' THEN 'model_call_failed' ELSE 'model_call_completed' END;

  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (
    p_tenant_id,
    p_run_id,
    event_name,
    jsonb_build_object(
      'task_id', p_task_id,
      'agent_id', p_agent_id,
      'model', p_model,
      'status', p_status,
      'latency_ms', p_latency_ms,
      'prompt_tokens', p_prompt_tokens,
      'completion_tokens', p_completion_tokens,
      'total_tokens', p_total_tokens,
      'estimated_cost_usd', p_estimated_cost_usd,
      'error', p_error
    ),
    p_actor
  );

  RETURN jsonb_build_object(
    'decision', 'recorded',
    'status', p_status,
    'event_type', event_name,
    'model', p_model,
    'task_id', p_task_id,
    'agent_id', p_agent_id,
    'estimated_cost_usd', p_estimated_cost_usd
  );
END;
$$;
