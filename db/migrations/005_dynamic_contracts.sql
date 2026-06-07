-- Dynamic contracts and Postgres-native governance improvements.

ALTER TABLE agent_tools
  ADD COLUMN IF NOT EXISTS output_schema jsonb NOT NULL DEFAULT '{"type":"object"}'::jsonb;

UPDATE agent_tools
SET output_schema = '{"type":"object"}'::jsonb
WHERE output_schema IS NULL;

CREATE OR REPLACE FUNCTION app.approval_policy_requires_approval(policy jsonb, arguments jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  rule jsonb;
  field_name text;
  op text;
  expected jsonb;
  actual jsonb;
  actual_num numeric;
  expected_num numeric;
BEGIN
  IF policy IS NULL OR policy = '{}'::jsonb THEN
    RETURN false;
  END IF;
  IF policy->>'mode' = 'always' THEN
    RETURN true;
  END IF;
  IF policy->>'mode' = 'never' THEN
    RETURN false;
  END IF;
  IF policy->>'risk_level' IN ('high', 'critical') THEN
    RETURN true;
  END IF;

  IF policy ? 'amount_threshold_cents' THEN
    field_name := COALESCE(policy->>'amount_argument', 'amount_cents');
    actual := arguments #> string_to_array(field_name, '.');
    IF jsonb_typeof(actual) = 'number' THEN
      actual_num := (actual #>> '{}')::numeric;
      expected_num := (policy->>'amount_threshold_cents')::numeric;
      IF actual_num >= expected_num THEN
        RETURN true;
      END IF;
    END IF;
  END IF;

  IF jsonb_typeof(policy->'rules') = 'array' THEN
    FOR rule IN SELECT value FROM jsonb_array_elements(policy->'rules') LOOP
      IF COALESCE(rule->>'decision', 'approval_required') <> 'approval_required' THEN
        CONTINUE;
      END IF;
      field_name := rule->>'field';
      IF field_name IS NULL THEN
        CONTINUE;
      END IF;
      actual := arguments #> string_to_array(field_name, '.');
      expected := rule->'value';
      op := COALESCE(rule->>'operator', 'eq');
      IF op = 'eq' AND actual = expected THEN
        RETURN true;
      END IF;
      IF jsonb_typeof(actual) = 'number' AND jsonb_typeof(expected) = 'number' THEN
        actual_num := (actual #>> '{}')::numeric;
        expected_num := (expected #>> '{}')::numeric;
        IF op IN ('gte', '>=') AND actual_num >= expected_num THEN RETURN true; END IF;
        IF op IN ('gt', '>') AND actual_num > expected_num THEN RETURN true; END IF;
        IF op IN ('lte', '<=') AND actual_num <= expected_num THEN RETURN true; END IF;
        IF op IN ('lt', '<') AND actual_num < expected_num THEN RETURN true; END IF;
      END IF;
    END LOOP;
  END IF;

  RETURN false;
EXCEPTION
  WHEN invalid_text_representation THEN
    RETURN false;
END;
$$;

CREATE OR REPLACE FUNCTION app.complete_tool_execution(
  p_tool_execution_id uuid,
  p_succeeded boolean,
  p_result jsonb DEFAULT '{}'::jsonb,
  p_error text DEFAULT NULL,
  p_actor text DEFAULT 'worker'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  execution_row tool_executions%ROWTYPE;
  tool_row agent_tools%ROWTYPE;
  next_status tool_execution_status;
BEGIN
  SELECT * INTO execution_row FROM tool_executions WHERE id = p_tool_execution_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('decision','missing_execution','status','failed');
  END IF;
  SELECT * INTO tool_row FROM agent_tools WHERE tenant_id = execution_row.tenant_id AND id = execution_row.tool_id;

  IF p_succeeded AND NOT app.jsonb_matches_schema(COALESCE(p_result->'output', '{}'::jsonb), tool_row.output_schema) THEN
    p_succeeded := false;
    p_error := format('tool output failed output_schema validation for %s', tool_row.name);
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (
      execution_row.tenant_id,
      execution_row.run_id,
      'tool_output_validation_failed',
      jsonb_build_object('task_id', execution_row.task_id, 'tool_execution_id', execution_row.id, 'tool_name', tool_row.name, 'output_schema', tool_row.output_schema),
      p_actor
    );
  END IF;

  next_status := CASE WHEN p_succeeded THEN 'completed'::tool_execution_status ELSE 'failed'::tool_execution_status END;
  UPDATE tool_executions
  SET status = next_status,
      result = CASE WHEN p_succeeded THEN p_result ELSE result END,
      error = CASE WHEN p_succeeded THEN NULL ELSE p_error END,
      completed_at = COALESCE(completed_at, now())
  WHERE id = p_tool_execution_id
  RETURNING * INTO execution_row;

  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (
    execution_row.tenant_id,
    execution_row.run_id,
    CASE WHEN p_succeeded THEN 'tool_completed' ELSE 'tool_failed' END,
    CASE WHEN p_succeeded
      THEN jsonb_build_object('task_id', execution_row.task_id, 'tool_execution_id', execution_row.id, 'tool_name', tool_row.name, 'result', p_result)
      ELSE jsonb_build_object('task_id', execution_row.task_id, 'tool_execution_id', execution_row.id, 'tool_name', tool_row.name, 'error', p_error)
    END,
    p_actor
  );

  IF execution_row.task_id IS NULL THEN
    UPDATE agent_runs SET status = 'queued' WHERE id = execution_row.run_id AND status = 'tool_running';
  ELSE
    UPDATE agent_tasks SET status = 'queued' WHERE id = execution_row.task_id AND status = 'tool_running';
  END IF;
  PERFORM app.send_agent_wakeup(execution_row.tenant_id, execution_row.run_id, execution_row.task_id);

  RETURN jsonb_build_object('decision','completed','status',next_status,'tool_execution_id',execution_row.id);
END;
$$;

CREATE OR REPLACE FUNCTION app.reserve_tool_execution(
  p_tenant_id uuid,
  p_run_id uuid,
  p_task_id uuid,
  p_agent_id uuid,
  p_tool_name text,
  p_arguments jsonb,
  p_force_approval boolean DEFAULT false,
  p_actor text DEFAULT 'worker'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  run_row agent_runs%ROWTYPE;
  task_row agent_tasks%ROWTYPE;
  tool_row agent_tools%ROWTYPE;
  execution_row tool_executions%ROWTYPE;
  approval_row approval_requests%ROWTYPE;
  owner_status text;
  v_arguments_hash text;
  v_idempotency_key text;
  requires_approval boolean;
BEGIN
  PERFORM app.validate_agent_command(jsonb_build_object('action','tool','tool_name',p_tool_name,'arguments',p_arguments), true);

  SELECT * INTO run_row FROM agent_runs WHERE tenant_id = p_tenant_id AND id = p_run_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('decision','missing_run','status','failed','reason','run_not_found');
  END IF;

  IF p_task_id IS NOT NULL THEN
    SELECT * INTO task_row FROM agent_tasks WHERE tenant_id = p_tenant_id AND id = p_task_id AND run_id = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
      RETURN jsonb_build_object('decision','missing_task','status','failed','reason','task_not_found');
    END IF;
    owner_status := task_row.status::text;
  ELSE
    owner_status := run_row.status::text;
  END IF;

  SELECT * INTO tool_row FROM agent_tools WHERE tenant_id = p_tenant_id AND name = p_tool_name AND enabled = true;
  IF NOT FOUND THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (p_tenant_id, p_run_id, 'tool_rejected', jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'tool_name', p_tool_name, 'reason', 'not_registered_or_disabled'), p_actor);
    RETURN jsonb_build_object('decision','rejected','status','failed','reason','not_registered_or_disabled');
  END IF;

  IF NOT app.tool_permission_allowed(p_tenant_id, tool_row.id, p_run_id, p_agent_id) THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (p_tenant_id, p_run_id, 'tool_permission_denied', jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'tool_name', p_tool_name, 'tool_id', tool_row.id), p_actor);
    RETURN jsonb_build_object('decision','permission_denied','status','failed','tool_id',tool_row.id);
  END IF;

  IF NOT app.jsonb_matches_schema(p_arguments, tool_row.input_schema) THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (p_tenant_id, p_run_id, 'tool_validation_failed', jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'tool_name', p_tool_name, 'tool_id', tool_row.id), p_actor);
    RETURN jsonb_build_object('decision','validation_failed','status','failed','tool_id',tool_row.id);
  END IF;

  v_arguments_hash := encode(digest(p_arguments::text, 'sha256'), 'hex');
  v_idempotency_key := encode(digest(jsonb_build_object(
    'tenant_id', p_tenant_id,
    'run_id', p_run_id,
    'task_id', p_task_id,
    'tool_name', p_tool_name,
    'arguments_hash', v_arguments_hash
  )::text, 'sha256'), 'hex');

  INSERT INTO tool_executions (tenant_id, run_id, task_id, tool_id, idempotency_key, arguments, arguments_hash)
  VALUES (p_tenant_id, p_run_id, p_task_id, tool_row.id, v_idempotency_key, p_arguments, v_arguments_hash)
  ON CONFLICT (tenant_id, tool_id, idempotency_key) DO NOTHING;

  SELECT * INTO execution_row
  FROM tool_executions
  WHERE tenant_id = p_tenant_id AND tool_id = tool_row.id AND tool_executions.idempotency_key = v_idempotency_key
  FOR UPDATE;

  requires_approval := tool_row.requires_approval OR p_force_approval OR app.approval_policy_requires_approval(tool_row.approval_policy, p_arguments);
  SELECT * INTO approval_row
  FROM approval_requests
  WHERE tenant_id = p_tenant_id AND tool_execution_id = execution_row.id
  ORDER BY created_at DESC
  LIMIT 1;

  IF requires_approval AND approval_row.id IS NULL THEN
    INSERT INTO approval_requests (tenant_id, run_id, task_id, tool_execution_id, reason, payload, requested_by)
    VALUES (
      p_tenant_id,
      p_run_id,
      p_task_id,
      execution_row.id,
      format('Approval required to execute tool %s.', p_tool_name),
      jsonb_build_object('tool_name', p_tool_name, 'arguments', p_arguments, 'idempotency_key', v_idempotency_key, 'task_id', p_task_id, 'approval_policy', tool_row.approval_policy),
      p_actor
    )
    RETURNING * INTO approval_row;

    UPDATE tool_executions SET status = 'waiting_approval' WHERE id = execution_row.id;
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (p_tenant_id, p_run_id, 'approval_requested', jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'approval_request_id', approval_row.id, 'tool_execution_id', execution_row.id, 'tool_name', p_tool_name, 'approval_policy', tool_row.approval_policy), p_actor);

    IF p_task_id IS NULL THEN
      UPDATE agent_runs SET status = 'waiting_approval' WHERE id = p_run_id AND status = owner_status::agent_run_status;
    ELSE
      UPDATE agent_tasks SET status = 'waiting_approval' WHERE id = p_task_id AND status = owner_status::agent_task_status;
    END IF;

    RETURN jsonb_build_object('decision','waiting_approval','status','waiting_approval','tool_execution_id',execution_row.id,'approval_request_id',approval_row.id,'idempotency_key',v_idempotency_key);
  END IF;

  IF approval_row.id IS NOT NULL AND approval_row.status = 'rejected' THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (p_tenant_id, p_run_id, 'approval_rejected', jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'approval_request_id', approval_row.id, 'tool_execution_id', execution_row.id), p_actor);
    IF p_task_id IS NULL THEN
      UPDATE agent_runs SET status = 'blocked', error = format('approval request %s was rejected', approval_row.id) WHERE id = p_run_id AND status = owner_status::agent_run_status;
    ELSE
      UPDATE agent_tasks SET status = 'blocked', error = format('approval request %s was rejected', approval_row.id) WHERE id = p_task_id AND status = owner_status::agent_task_status;
    END IF;
    RETURN jsonb_build_object('decision','blocked','status','blocked','tool_execution_id',execution_row.id,'approval_request_id',approval_row.id);
  END IF;

  IF requires_approval AND approval_row.status <> 'approved' THEN
    IF p_task_id IS NULL THEN
      UPDATE agent_runs SET status = 'waiting_approval' WHERE id = p_run_id AND status = owner_status::agent_run_status;
    ELSE
      UPDATE agent_tasks SET status = 'waiting_approval' WHERE id = p_task_id AND status = owner_status::agent_task_status;
    END IF;
    RETURN jsonb_build_object('decision','waiting_approval','status','waiting_approval','tool_execution_id',execution_row.id,'approval_request_id',approval_row.id);
  END IF;

  IF execution_row.status = 'completed' THEN
    PERFORM app.advance_tool_owner_to_running(p_run_id, p_task_id, owner_status);
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (p_tenant_id, p_run_id, 'tool_execution_replayed', jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'tool_execution_id', execution_row.id, 'tool_name', p_tool_name, 'result', execution_row.result), p_actor);
    IF p_task_id IS NULL THEN
      UPDATE agent_runs SET status = 'queued' WHERE id = p_run_id AND status = 'tool_running';
    ELSE
      UPDATE agent_tasks SET status = 'queued' WHERE id = p_task_id AND status = 'tool_running';
    END IF;
    PERFORM app.send_agent_wakeup(p_tenant_id, p_run_id, p_task_id);
    RETURN jsonb_build_object('decision','replayed','status','completed','tool_execution_id',execution_row.id,'result',execution_row.result);
  END IF;

  IF execution_row.status = 'failed' THEN
    PERFORM app.advance_tool_owner_to_running(p_run_id, p_task_id, owner_status);
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (p_tenant_id, p_run_id, 'tool_execution_replayed', jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'tool_execution_id', execution_row.id, 'tool_name', p_tool_name, 'error', execution_row.error), p_actor);
    IF p_task_id IS NULL THEN
      UPDATE agent_runs SET status = 'queued' WHERE id = p_run_id AND status = 'tool_running';
    ELSE
      UPDATE agent_tasks SET status = 'queued' WHERE id = p_task_id AND status = 'tool_running';
    END IF;
    PERFORM app.send_agent_wakeup(p_tenant_id, p_run_id, p_task_id);
    RETURN jsonb_build_object('decision','replayed','status','failed','tool_execution_id',execution_row.id,'error',execution_row.error);
  END IF;

  IF execution_row.status = 'running' THEN
    PERFORM app.advance_tool_owner_to_running(p_run_id, p_task_id, owner_status);
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (p_tenant_id, p_run_id, 'tool_execution_in_progress', jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'tool_execution_id', execution_row.id, 'tool_name', p_tool_name), p_actor);
    IF p_task_id IS NULL THEN
      UPDATE agent_runs SET status = 'queued' WHERE id = p_run_id AND status = 'tool_running';
    ELSE
      UPDATE agent_tasks SET status = 'queued' WHERE id = p_task_id AND status = 'tool_running';
    END IF;
    PERFORM app.send_agent_wakeup(p_tenant_id, p_run_id, p_task_id);
    RETURN jsonb_build_object('decision','in_progress','status','running','tool_execution_id',execution_row.id);
  END IF;

  PERFORM app.advance_tool_owner_to_running(p_run_id, p_task_id, owner_status);
  UPDATE tool_executions SET status = 'running', started_at = COALESCE(started_at, now()) WHERE id = execution_row.id RETURNING * INTO execution_row;
  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (p_tenant_id, p_run_id, 'tool_started', jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'tool_execution_id', execution_row.id, 'tool_name', p_tool_name, 'arguments', p_arguments), p_actor);

  RETURN jsonb_build_object('decision','execute_tool','status','running','tool_execution_id',execution_row.id,'tool_id',tool_row.id,'idempotency_key',v_idempotency_key,'arguments',p_arguments);
END;
$$;

CREATE INDEX IF NOT EXISTS idx_agent_events_fts
  ON agent_events USING gin (to_tsvector('simple', event_type || ' ' || payload::text));

CREATE INDEX IF NOT EXISTS idx_agent_memory_fts
  ON agent_memory USING gin (to_tsvector('simple', memory_type || ' ' || content || ' ' || metadata::text));

CREATE INDEX IF NOT EXISTS idx_agent_tools_fts
  ON agent_tools USING gin (to_tsvector('simple', name || ' ' || description || ' ' || input_schema::text || ' ' || output_schema::text || ' ' || approval_policy::text));
