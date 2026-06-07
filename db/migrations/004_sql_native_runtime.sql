-- SQL-native runtime APIs for the Postgres agent harness.
-- Postgres governs command contracts, tool reservations, approvals, replay, and search.

CREATE OR REPLACE FUNCTION app.jsonb_matches_schema(p_value jsonb, schema jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  schema_type text;
  required_key text;
  prop record;
  prop_schema jsonb;
  prop_type text;
  enum_values jsonb;
BEGIN
  IF schema IS NULL OR schema = '{}'::jsonb THEN
    RETURN jsonb_typeof(p_value) = 'object';
  END IF;

  schema_type := schema->>'type';
  IF schema_type IS NOT NULL THEN
    IF schema_type = 'object' AND jsonb_typeof(p_value) <> 'object' THEN RETURN false; END IF;
    IF schema_type = 'array' AND jsonb_typeof(p_value) <> 'array' THEN RETURN false; END IF;
    IF schema_type = 'string' AND jsonb_typeof(p_value) <> 'string' THEN RETURN false; END IF;
    IF schema_type = 'number' AND jsonb_typeof(p_value) <> 'number' THEN RETURN false; END IF;
    IF schema_type = 'integer' AND jsonb_typeof(p_value) <> 'number' THEN RETURN false; END IF;
    IF schema_type = 'boolean' AND jsonb_typeof(p_value) <> 'boolean' THEN RETURN false; END IF;
  END IF;

  IF jsonb_typeof(schema->'enum') = 'array' THEN
    enum_values := schema->'enum';
    IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(enum_values) AS item(v) WHERE item.v = p_value) THEN
      RETURN false;
    END IF;
  END IF;

  IF schema_type = 'object' OR schema ? 'properties' OR schema ? 'required' THEN
    IF jsonb_typeof(p_value) <> 'object' THEN
      RETURN false;
    END IF;

    IF jsonb_typeof(schema->'required') = 'array' THEN
      FOR required_key IN SELECT jsonb_array_elements_text(schema->'required') LOOP
        IF NOT p_value ? required_key THEN
          RETURN false;
        END IF;
      END LOOP;
    END IF;

    IF COALESCE((schema->>'additionalProperties')::boolean, true) = false THEN
      IF EXISTS (
        SELECT 1
        FROM jsonb_object_keys(p_value) AS key_name(k)
        WHERE NOT (COALESCE(schema->'properties', '{}'::jsonb) ? key_name.k)
      ) THEN
        RETURN false;
      END IF;
    END IF;

    FOR prop IN SELECT prop_item.key, prop_item.value AS schema_value FROM jsonb_each(COALESCE(schema->'properties', '{}'::jsonb)) AS prop_item(key, value) LOOP
      IF p_value ? prop.key THEN
        prop_schema := prop.schema_value;
        prop_type := prop_schema->>'type';
        IF prop_type IS NOT NULL AND NOT app.jsonb_matches_schema(p_value->prop.key, prop_schema) THEN
          RETURN false;
        END IF;
        IF prop_type IS NULL AND jsonb_typeof(prop_schema->'enum') = 'array' THEN
          IF NOT app.jsonb_matches_schema(p_value->prop.key, prop_schema) THEN
            RETURN false;
          END IF;
        END IF;
      END IF;
    END LOOP;
  END IF;

  RETURN true;
EXCEPTION
  WHEN invalid_text_representation THEN
    RETURN false;
END;
$$;

CREATE OR REPLACE FUNCTION app.validate_agent_command(command jsonb, allow_delegate boolean DEFAULT true)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  action text;
  expected_keys text[];
  actual_keys text[];
BEGIN
  IF command IS NULL OR jsonb_typeof(command) <> 'object' THEN
    RAISE EXCEPTION 'model output must be exactly one JSON object' USING ERRCODE = '22023';
  END IF;

  action := command->>'action';
  IF action IS NULL OR action NOT IN ('final', 'tool', 'ask_human', 'remember', 'delegate', 'fail') THEN
    RAISE EXCEPTION 'command action is missing or unsupported' USING ERRCODE = '22023';
  END IF;
  IF action = 'delegate' AND NOT allow_delegate THEN
    RAISE EXCEPTION 'delegate commands are not allowed in this context' USING ERRCODE = '22023';
  END IF;

  expected_keys := CASE action
    WHEN 'final' THEN ARRAY['action','answer']
    WHEN 'tool' THEN ARRAY['action','tool_name','arguments']
    WHEN 'ask_human' THEN ARRAY['action','reason','payload']
    WHEN 'remember' THEN ARRAY['action','memory_type','content','metadata']
    WHEN 'delegate' THEN ARRAY['action','to_agent','task','reason']
    WHEN 'fail' THEN ARRAY['action','reason']
  END;

  SELECT array_agg(k ORDER BY k) INTO actual_keys FROM jsonb_object_keys(command) AS k;
  IF actual_keys IS DISTINCT FROM (SELECT array_agg(k ORDER BY k) FROM unnest(expected_keys) AS k) THEN
    RAISE EXCEPTION 'command keys do not match contract' USING ERRCODE = '22023';
  END IF;

  IF action = 'final' AND jsonb_typeof(command->'answer') <> 'object' THEN
    RAISE EXCEPTION 'final.answer must be an object' USING ERRCODE = '22023';
  ELSIF action = 'tool' THEN
    IF COALESCE(command->>'tool_name', '') !~ '^[a-z][a-z0-9_]*$' THEN
      RAISE EXCEPTION 'tool.tool_name must be snake_case' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(command->'arguments') <> 'object' THEN
      RAISE EXCEPTION 'tool.arguments must be an object' USING ERRCODE = '22023';
    END IF;
  ELSIF action = 'ask_human' THEN
    IF length(btrim(COALESCE(command->>'reason', ''))) = 0 THEN
      RAISE EXCEPTION 'ask_human.reason must be a non-empty string' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(command->'payload') <> 'object' THEN
      RAISE EXCEPTION 'ask_human.payload must be an object' USING ERRCODE = '22023';
    END IF;
  ELSIF action = 'remember' THEN
    IF length(btrim(COALESCE(command->>'memory_type', ''))) = 0 THEN
      RAISE EXCEPTION 'remember.memory_type must be a non-empty string' USING ERRCODE = '22023';
    END IF;
    IF length(btrim(COALESCE(command->>'content', ''))) = 0 THEN
      RAISE EXCEPTION 'remember.content must be a non-empty string' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(command->'metadata') <> 'object' THEN
      RAISE EXCEPTION 'remember.metadata must be an object' USING ERRCODE = '22023';
    END IF;
  ELSIF action = 'delegate' THEN
    IF COALESCE(command->>'to_agent', '') !~ '^[a-z][a-z0-9_]*$' THEN
      RAISE EXCEPTION 'delegate.to_agent must be snake_case' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(command->'task') <> 'object' THEN
      RAISE EXCEPTION 'delegate.task must be an object' USING ERRCODE = '22023';
    END IF;
    IF length(btrim(COALESCE(command->>'reason', ''))) = 0 THEN
      RAISE EXCEPTION 'delegate.reason must be a non-empty string' USING ERRCODE = '22023';
    END IF;
  ELSIF action = 'fail' THEN
    IF length(btrim(COALESCE(command->>'reason', ''))) = 0 THEN
      RAISE EXCEPTION 'fail.reason must be a non-empty string' USING ERRCODE = '22023';
    END IF;
  END IF;

  RETURN action;
END;
$$;

CREATE OR REPLACE FUNCTION app.tool_permission_allowed(
  p_tenant_id uuid,
  p_tool_id uuid,
  p_run_id uuid,
  p_agent_id uuid DEFAULT NULL
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
  SELECT COALESCE((
    SELECT allowed
    FROM agent_tool_permissions
    WHERE tenant_id = p_tenant_id
      AND tool_id = p_tool_id
      AND (
        (subject_type = 'run' AND subject_id = p_run_id::text)
        OR (p_agent_id IS NOT NULL AND subject_type = 'agent' AND subject_id = p_agent_id::text)
        OR (subject_type = 'tenant' AND subject_id = p_tenant_id::text)
      )
    ORDER BY CASE subject_type
      WHEN 'run' THEN 0
      WHEN 'agent' THEN 1
      WHEN 'tenant' THEN 2
      ELSE 3
    END
    LIMIT 1
  ), false);
$$;

CREATE OR REPLACE FUNCTION app.send_agent_wakeup(p_tenant_id uuid, p_run_id uuid, p_task_id uuid DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  payload jsonb;
BEGIN
  payload := jsonb_build_object('tenant_id', p_tenant_id, 'run_id', p_run_id);
  IF p_task_id IS NOT NULL THEN
    payload := payload || jsonb_build_object('task_id', p_task_id);
  END IF;
  PERFORM pgmq.send('agent_wakeups', payload);
EXCEPTION
  WHEN undefined_function OR undefined_table THEN
    RAISE NOTICE 'pgmq queue agent_wakeups is unavailable; wakeup skipped';
END;
$$;

CREATE OR REPLACE FUNCTION app.advance_tool_owner_to_running(
  p_run_id uuid,
  p_task_id uuid,
  p_current_status text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
  IF p_task_id IS NULL THEN
    UPDATE agent_runs SET status = 'needs_tool' WHERE id = p_run_id AND status = p_current_status::agent_run_status;
    UPDATE agent_runs SET status = 'tool_running' WHERE id = p_run_id AND status = 'needs_tool';
  ELSE
    UPDATE agent_tasks SET status = 'needs_tool' WHERE id = p_task_id AND status = p_current_status::agent_task_status;
    UPDATE agent_tasks SET status = 'tool_running' WHERE id = p_task_id AND status = 'needs_tool';
  END IF;
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

  requires_approval := tool_row.requires_approval OR p_force_approval;
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
      jsonb_build_object('tool_name', p_tool_name, 'arguments', p_arguments, 'idempotency_key', v_idempotency_key, 'task_id', p_task_id),
      p_actor
    )
    RETURNING * INTO approval_row;

    UPDATE tool_executions SET status = 'waiting_approval' WHERE id = execution_row.id;
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (p_tenant_id, p_run_id, 'approval_requested', jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'approval_request_id', approval_row.id, 'tool_execution_id', execution_row.id, 'tool_name', p_tool_name), p_actor);

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

CREATE OR REPLACE FUNCTION app.submit_agent_command(
  p_tenant_id uuid,
  p_run_id uuid,
  p_task_id uuid,
  p_agent_id uuid,
  p_command jsonb,
  p_actor text DEFAULT 'worker'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  action text;
  run_row agent_runs%ROWTYPE;
  memory_id uuid;
  approval_id uuid;
BEGIN
  action := app.validate_agent_command(p_command, p_task_id IS NOT NULL);
  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (p_tenant_id, p_run_id, 'llm_command_received', p_command || jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id), p_actor);

  IF action = 'tool' THEN
    RETURN app.reserve_tool_execution(p_tenant_id, p_run_id, p_task_id, p_agent_id, p_command->>'tool_name', p_command->'arguments', false, p_actor);
  END IF;

  IF p_task_id IS NOT NULL THEN
    RETURN jsonb_build_object('decision','python_task_handler_required','status','thinking','action',action);
  END IF;

  SELECT * INTO run_row FROM agent_runs WHERE tenant_id = p_tenant_id AND id = p_run_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('decision','missing_run','status','failed');
  END IF;

  IF action = 'final' THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor) VALUES (p_tenant_id, p_run_id, 'run_completed', p_command->'answer', p_actor);
    UPDATE agent_runs SET status = 'completed', answer = p_command->'answer', completed_at = COALESCE(completed_at, now()) WHERE id = p_run_id AND status = run_row.status;
    RETURN jsonb_build_object('decision','completed','status','completed');
  ELSIF action = 'fail' THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor) VALUES (p_tenant_id, p_run_id, 'run_failed', jsonb_build_object('reason', p_command->>'reason'), p_actor);
    UPDATE agent_runs SET status = 'failed', error = p_command->>'reason', completed_at = COALESCE(completed_at, now()) WHERE id = p_run_id AND status = run_row.status;
    RETURN jsonb_build_object('decision','failed','status','failed');
  ELSIF action = 'ask_human' THEN
    INSERT INTO approval_requests (tenant_id, run_id, reason, payload, requested_by)
    VALUES (p_tenant_id, p_run_id, p_command->>'reason', p_command->'payload', p_actor)
    RETURNING id INTO approval_id;
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor) VALUES (p_tenant_id, p_run_id, 'approval_requested', jsonb_build_object('approval_request_id', approval_id, 'payload', p_command->'payload'), p_actor);
    UPDATE agent_runs SET status = 'waiting_approval' WHERE id = p_run_id AND status = run_row.status;
    RETURN jsonb_build_object('decision','waiting_approval','status','waiting_approval','approval_request_id',approval_id);
  ELSIF action = 'remember' THEN
    UPDATE agent_runs SET status = 'needs_tool' WHERE id = p_run_id AND status = run_row.status;
    UPDATE agent_runs SET status = 'tool_running' WHERE id = p_run_id AND status = 'needs_tool';
    INSERT INTO agent_memory (tenant_id, memory_type, content, metadata, source_run_id)
    VALUES (p_tenant_id, p_command->>'memory_type', p_command->>'content', p_command->'metadata', p_run_id)
    RETURNING id INTO memory_id;
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor) VALUES (p_tenant_id, p_run_id, 'memory_recorded', jsonb_build_object('memory_id', memory_id, 'memory_type', p_command->>'memory_type'), p_actor);
    UPDATE agent_runs SET status = 'queued' WHERE id = p_run_id AND status = 'tool_running';
    PERFORM app.send_agent_wakeup(p_tenant_id, p_run_id, NULL);
    RETURN jsonb_build_object('decision','queued','status','queued','memory_id',memory_id);
  ELSIF action = 'delegate' THEN
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor) VALUES (p_tenant_id, p_run_id, 'run_failed', jsonb_build_object('reason','delegate commands require AgentTaskWorker'), p_actor);
    UPDATE agent_runs SET status = 'failed', error = 'delegate commands require AgentTaskWorker', completed_at = COALESCE(completed_at, now()) WHERE id = p_run_id AND status = run_row.status;
    RETURN jsonb_build_object('decision','failed','status','failed');
  END IF;

  RETURN jsonb_build_object('decision','unsupported','status','failed');
END;
$$;

CREATE OR REPLACE FUNCTION app.resolve_approval_request(
  p_approval_id uuid,
  p_approved boolean,
  p_resolved_by text DEFAULT 'human'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  approval_row approval_requests%ROWTYPE;
  next_status approval_status;
BEGIN
  next_status := CASE WHEN p_approved THEN 'approved'::approval_status ELSE 'rejected'::approval_status END;
  UPDATE approval_requests
  SET status = next_status, resolved_by = p_resolved_by, resolved_at = now()
  WHERE id = p_approval_id AND status = 'pending'
  RETURNING * INTO approval_row;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('decision','not_pending','status','failed');
  END IF;

  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (approval_row.tenant_id, approval_row.run_id, 'approval_resolved', jsonb_build_object('approval_request_id', approval_row.id, 'status', next_status, 'resolved_by', p_resolved_by, 'task_id', approval_row.task_id), p_resolved_by);

  IF approval_row.task_id IS NULL THEN
    IF p_approved THEN
      UPDATE agent_runs SET status = 'queued' WHERE id = approval_row.run_id AND status = 'waiting_approval';
      PERFORM app.send_agent_wakeup(approval_row.tenant_id, approval_row.run_id, NULL);
    ELSE
      UPDATE agent_runs SET status = 'blocked', error = format('approval request %s was rejected', approval_row.id) WHERE id = approval_row.run_id AND status = 'waiting_approval';
    END IF;
  ELSE
    IF p_approved THEN
      UPDATE agent_tasks SET status = 'queued' WHERE id = approval_row.task_id AND status = 'waiting_approval';
      PERFORM app.send_agent_wakeup(approval_row.tenant_id, approval_row.run_id, approval_row.task_id);
    ELSE
      UPDATE agent_tasks SET status = 'blocked', error = format('approval request %s was rejected', approval_row.id) WHERE id = approval_row.task_id AND status = 'waiting_approval';
    END IF;
  END IF;

  RETURN jsonb_build_object('decision','resolved','status',next_status,'approval_request_id',approval_row.id,'task_id',approval_row.task_id);
END;
$$;

CREATE OR REPLACE VIEW app.run_trajectory_v
WITH (security_invoker = true)
AS
SELECT
  e.tenant_id,
  e.run_id,
  e.event_id AS sequence_id,
  e.created_at,
  'event'::text AS source,
  e.event_type AS step_type,
  e.actor,
  e.payload
FROM agent_events e
UNION ALL
SELECT
  m.tenant_id,
  m.run_id,
  (1000000000000 + row_number() OVER (PARTITION BY m.tenant_id, m.run_id ORDER BY m.created_at, m.id))::bigint AS sequence_id,
  m.created_at,
  'message'::text AS source,
  m.message_type AS step_type,
  'agent'::text AS actor,
  jsonb_build_object('message_id', m.id, 'from_task_id', m.from_task_id, 'to_task_id', m.to_task_id, 'content', m.content) AS payload
FROM agent_messages m;

CREATE OR REPLACE FUNCTION app.search_harness(
  p_tenant_id uuid,
  p_query text,
  p_limit integer DEFAULT 50
)
RETURNS TABLE(
  source text,
  id text,
  run_id uuid,
  created_at timestamptz,
  rank real,
  snippet text,
  payload jsonb
)
LANGUAGE sql
STABLE
AS $$
  WITH q AS (SELECT websearch_to_tsquery('simple', COALESCE(NULLIF(p_query, ''), ' ')) AS query)
  SELECT * FROM (
    SELECT
      'event'::text AS source,
      e.event_id::text AS id,
      e.run_id,
      e.created_at,
      ts_rank_cd(to_tsvector('simple', e.event_type || ' ' || e.payload::text), q.query) AS rank,
      e.event_type AS snippet,
      e.payload
    FROM agent_events e, q
    WHERE e.tenant_id = p_tenant_id
      AND to_tsvector('simple', e.event_type || ' ' || e.payload::text) @@ q.query
    UNION ALL
    SELECT
      'memory'::text,
      m.id::text,
      m.source_run_id,
      m.created_at,
      ts_rank_cd(to_tsvector('simple', m.memory_type || ' ' || m.content || ' ' || m.metadata::text), q.query),
      left(m.content, 240),
      jsonb_build_object('memory_type', m.memory_type, 'metadata', m.metadata)
    FROM agent_memory m, q
    WHERE m.tenant_id = p_tenant_id
      AND to_tsvector('simple', m.memory_type || ' ' || m.content || ' ' || m.metadata::text) @@ q.query
    UNION ALL
    SELECT
      'tool'::text,
      t.id::text,
      NULL::uuid,
      t.created_at,
      ts_rank_cd(to_tsvector('simple', t.name || ' ' || t.description || ' ' || t.input_schema::text), q.query),
      t.name,
      jsonb_build_object('enabled', t.enabled, 'requires_approval', t.requires_approval, 'input_schema', t.input_schema)
    FROM agent_tools t, q
    WHERE t.tenant_id = p_tenant_id
      AND to_tsvector('simple', t.name || ' ' || t.description || ' ' || t.input_schema::text) @@ q.query
    UNION ALL
    SELECT
      'eval'::text,
      er.id::text,
      er.run_id,
      er.created_at,
      ts_rank_cd(to_tsvector('simple', er.scores::text), q.query),
      ec.name,
      er.scores
    FROM eval_results er
    JOIN eval_cases ec ON ec.tenant_id = er.tenant_id AND ec.id = er.eval_case_id,
      q
    WHERE er.tenant_id = p_tenant_id
      AND to_tsvector('simple', ec.name || ' ' || er.scores::text) @@ q.query
  ) results
  ORDER BY rank DESC, created_at DESC
  LIMIT LEAST(GREATEST(p_limit, 1), 100);
$$;
