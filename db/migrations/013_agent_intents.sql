-- Framework-facing planner intents.
-- Intents are external proposals. Commands remain the canonical internal
-- governed execution unit.

CREATE OR REPLACE FUNCTION app.agent_intent_event_payload(p_intent jsonb)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT COALESCE(p_intent, '{}'::jsonb)
    - 'answer'
    - 'arguments'
    - 'payload'
    - 'content'
    - 'metadata'
    - 'task'
    - 'secret'
    - 'api_key'
    - 'token'
    - 'password';
$$;

CREATE OR REPLACE FUNCTION app.validate_agent_intent(intent jsonb, allow_delegate boolean DEFAULT true)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  intent_name text;
  expected_keys text[];
  required_keys text[];
  actual_keys text[];
  schema_version integer;
BEGIN
  IF intent IS NULL OR jsonb_typeof(intent) <> 'object' THEN
    RAISE EXCEPTION 'intent output must be exactly one JSON object' USING ERRCODE = '22023';
  END IF;
  IF intent ? 'tool_calls' OR intent ? 'function_call' THEN
    RAISE EXCEPTION 'framework-native tool calls are not Rowplane intents' USING ERRCODE = '22023';
  END IF;

  schema_version := (intent->>'schema_version')::integer;
  IF schema_version IS DISTINCT FROM 1 THEN
    RAISE EXCEPTION 'unsupported intent.schema_version' USING ERRCODE = '22023';
  END IF;

  intent_name := intent->>'intent';
  IF intent_name IS NULL OR intent_name NOT IN (
    'final_answer',
    'tool_request',
    'clarification_request',
    'memory_proposal',
    'delegation_request',
    'failure'
  ) THEN
    RAISE EXCEPTION 'intent is missing or unsupported' USING ERRCODE = '22023';
  END IF;
  IF intent_name = 'delegation_request' AND NOT allow_delegate THEN
    RAISE EXCEPTION 'delegation_request intents are not allowed in this context' USING ERRCODE = '22023';
  END IF;

  required_keys := CASE intent_name
    WHEN 'final_answer' THEN ARRAY['schema_version','intent','answer']
    WHEN 'tool_request' THEN ARRAY['schema_version','intent','tool_name','arguments']
    WHEN 'clarification_request' THEN ARRAY['schema_version','intent','reason','payload']
    WHEN 'memory_proposal' THEN ARRAY['schema_version','intent','memory_type','content','metadata']
    WHEN 'delegation_request' THEN ARRAY['schema_version','intent','to_agent','task','reason']
    WHEN 'failure' THEN ARRAY['schema_version','intent','reason']
  END;
  expected_keys := required_keys || ARRAY['intent_id'];

  SELECT array_agg(k ORDER BY k) INTO actual_keys FROM jsonb_object_keys(intent) AS k;
  IF NOT (
    actual_keys <@ (SELECT array_agg(k ORDER BY k) FROM unnest(expected_keys) AS k)
    AND (SELECT array_agg(k ORDER BY k) FROM unnest(required_keys) AS k) <@ actual_keys
  ) THEN
    RAISE EXCEPTION 'intent keys do not match contract' USING ERRCODE = '22023';
  END IF;

  IF intent ? 'intent_id' AND COALESCE(intent->>'intent_id', '') !~ '^[A-Za-z0-9_.:-]{1,128}$' THEN
    RAISE EXCEPTION 'intent.intent_id must be a stable short string' USING ERRCODE = '22023';
  END IF;

  IF intent_name = 'final_answer' AND jsonb_typeof(intent->'answer') <> 'object' THEN
    RAISE EXCEPTION 'final_answer.answer must be an object' USING ERRCODE = '22023';
  ELSIF intent_name = 'tool_request' THEN
    IF COALESCE(intent->>'tool_name', '') !~ '^[a-z][a-z0-9_]*$' THEN
      RAISE EXCEPTION 'tool_request.tool_name must be snake_case' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(intent->'arguments') <> 'object' THEN
      RAISE EXCEPTION 'tool_request.arguments must be an object' USING ERRCODE = '22023';
    END IF;
  ELSIF intent_name = 'clarification_request' THEN
    IF length(btrim(COALESCE(intent->>'reason', ''))) = 0 THEN
      RAISE EXCEPTION 'clarification_request.reason must be a non-empty string' USING ERRCODE = '22023';
    END IF;
    IF lower(intent->>'reason') LIKE '%requires approval%' OR lower(intent->>'reason') LIKE '%approval required%' THEN
      RAISE EXCEPTION 'adapters must not decide approval requirements' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(intent->'payload') <> 'object' THEN
      RAISE EXCEPTION 'clarification_request.payload must be an object' USING ERRCODE = '22023';
    END IF;
  ELSIF intent_name = 'memory_proposal' THEN
    IF length(btrim(COALESCE(intent->>'memory_type', ''))) = 0 THEN
      RAISE EXCEPTION 'memory_proposal.memory_type must be a non-empty string' USING ERRCODE = '22023';
    END IF;
    IF length(btrim(COALESCE(intent->>'content', ''))) = 0 THEN
      RAISE EXCEPTION 'memory_proposal.content must be a non-empty string' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(intent->'metadata') <> 'object' THEN
      RAISE EXCEPTION 'memory_proposal.metadata must be an object' USING ERRCODE = '22023';
    END IF;
  ELSIF intent_name = 'delegation_request' THEN
    IF COALESCE(intent->>'to_agent', '') !~ '^[a-z][a-z0-9_]*$' THEN
      RAISE EXCEPTION 'delegation_request.to_agent must be snake_case' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(intent->'task') <> 'object' THEN
      RAISE EXCEPTION 'delegation_request.task must be an object' USING ERRCODE = '22023';
    END IF;
    IF length(btrim(COALESCE(intent->>'reason', ''))) = 0 THEN
      RAISE EXCEPTION 'delegation_request.reason must be a non-empty string' USING ERRCODE = '22023';
    END IF;
  ELSIF intent_name = 'failure' THEN
    IF length(btrim(COALESCE(intent->>'reason', ''))) = 0 THEN
      RAISE EXCEPTION 'failure.reason must be a non-empty string' USING ERRCODE = '22023';
    END IF;
  END IF;

  RETURN intent_name;
EXCEPTION
  WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'unsupported intent.schema_version' USING ERRCODE = '22023';
END;
$$;

CREATE OR REPLACE FUNCTION app.simulate_agent_intent_policy(
  p_tenant_id uuid,
  p_run_id uuid,
  p_task_id uuid,
  p_agent_id uuid,
  p_intent jsonb,
  p_actor text DEFAULT 'worker'
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  intent_name text;
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
  BEGIN
    intent_name := app.validate_agent_intent(p_intent, p_task_id IS NOT NULL);
  EXCEPTION WHEN OTHERS THEN
    RETURN jsonb_build_object('decision','invalid','status','failed','reason',SQLERRM);
  END;

  SELECT * INTO run_row FROM agent_runs WHERE tenant_id = p_tenant_id AND id = p_run_id;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('decision','blocked','status','failed','reason','run_not_found');
  END IF;

  IF p_task_id IS NOT NULL THEN
    SELECT * INTO task_row FROM agent_tasks WHERE tenant_id = p_tenant_id AND id = p_task_id AND run_id = p_run_id;
    IF NOT FOUND THEN
      RETURN jsonb_build_object('decision','blocked','status','failed','reason','task_not_found');
    END IF;
    owner_status := task_row.status::text;
  ELSE
    owner_status := run_row.status::text;
  END IF;

  IF owner_status IN ('completed', 'failed') THEN
    RETURN jsonb_build_object('decision','terminal','status',owner_status);
  END IF;
  IF owner_status = 'blocked' THEN
    RETURN jsonb_build_object('decision','blocked','status','blocked');
  END IF;

  IF intent_name IN ('final_answer', 'failure') THEN
    RETURN jsonb_build_object('decision','terminal','status','allowed','intent',intent_name);
  END IF;
  IF intent_name IN ('clarification_request', 'memory_proposal', 'delegation_request') THEN
    RETURN jsonb_build_object('decision','allowed','status','allowed','intent',intent_name);
  END IF;

  SELECT * INTO tool_row
  FROM agent_tools
  WHERE tenant_id = p_tenant_id AND name = p_intent->>'tool_name' AND enabled = true;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('decision','denied','status','failed','reason','not_registered_or_disabled');
  END IF;

  IF NOT app.tool_permission_allowed(p_tenant_id, tool_row.id, p_run_id, p_agent_id) THEN
    RETURN jsonb_build_object('decision','denied','status','failed','reason','permission_denied','tool_id',tool_row.id);
  END IF;

  IF NOT app.jsonb_matches_schema(p_intent->'arguments', tool_row.input_schema) THEN
    RETURN jsonb_build_object('decision','invalid','status','failed','reason','tool_schema_validation_failed','tool_id',tool_row.id);
  END IF;

  v_arguments_hash := encode(digest((p_intent->'arguments')::text, 'sha256'), 'hex');
  v_idempotency_key := encode(digest(jsonb_build_object(
    'tenant_id', p_tenant_id,
    'run_id', p_run_id,
    'task_id', p_task_id,
    'tool_name', p_intent->>'tool_name',
    'arguments_hash', v_arguments_hash
  )::text, 'sha256'), 'hex');

  SELECT * INTO execution_row
  FROM tool_executions
  WHERE tenant_id = p_tenant_id AND tool_id = tool_row.id AND idempotency_key = v_idempotency_key
  ORDER BY created_at DESC
  LIMIT 1;

  IF FOUND THEN
    SELECT * INTO approval_row
    FROM approval_requests
    WHERE tenant_id = p_tenant_id AND tool_execution_id = execution_row.id
    ORDER BY created_at DESC
    LIMIT 1;
    IF approval_row.id IS NOT NULL AND approval_row.status = 'rejected' THEN
      RETURN jsonb_build_object('decision','blocked','status','blocked','reason','approval_rejected','tool_execution_id',execution_row.id,'approval_request_id',approval_row.id);
    END IF;
    IF execution_row.status IN ('completed', 'failed', 'running') THEN
      RETURN jsonb_build_object('decision','idempotent_replay','status',execution_row.status,'tool_execution_id',execution_row.id,'idempotency_key',v_idempotency_key);
    END IF;
    IF execution_row.status = 'waiting_approval' OR approval_row.status = 'pending' THEN
      RETURN jsonb_build_object('decision','requires_approval','status','waiting_approval','tool_execution_id',execution_row.id,'approval_request_id',approval_row.id,'idempotency_key',v_idempotency_key);
    END IF;
  END IF;

  requires_approval := tool_row.requires_approval OR app.approval_policy_requires_approval(tool_row.approval_policy, p_intent->'arguments');
  IF requires_approval THEN
    RETURN jsonb_build_object('decision','requires_approval','status','waiting_approval','tool_id',tool_row.id,'idempotency_key',v_idempotency_key);
  END IF;

  RETURN jsonb_build_object('decision','allowed','status','running','tool_id',tool_row.id,'idempotency_key',v_idempotency_key);
END;
$$;

CREATE OR REPLACE FUNCTION app.submit_agent_intent(
  p_tenant_id uuid,
  p_run_id uuid,
  p_task_id uuid,
  p_agent_id uuid,
  p_intent jsonb,
  p_actor text DEFAULT 'worker'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  intent_name text;
  decision jsonb;
  command jsonb;
BEGIN
  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (
    p_tenant_id,
    p_run_id,
    'llm_intent_received',
    app.agent_intent_event_payload(p_intent) || jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id),
    p_actor
  );

  decision := app.simulate_agent_intent_policy(p_tenant_id, p_run_id, p_task_id, p_agent_id, p_intent, p_actor);
  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (
    p_tenant_id,
    p_run_id,
    'intent_decision_recorded',
    decision || jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id),
    p_actor
  );

  IF decision->>'decision' IN ('invalid', 'denied', 'blocked') THEN
    RETURN decision;
  END IF;

  intent_name := app.validate_agent_intent(p_intent, p_task_id IS NOT NULL);
  command := CASE intent_name
    WHEN 'final_answer' THEN jsonb_build_object('action','final','answer',p_intent->'answer')
    WHEN 'tool_request' THEN jsonb_build_object('action','tool','tool_name',p_intent->>'tool_name','arguments',p_intent->'arguments')
    WHEN 'clarification_request' THEN jsonb_build_object('action','ask_human','reason',p_intent->>'reason','payload',p_intent->'payload')
    WHEN 'memory_proposal' THEN jsonb_build_object('action','remember','memory_type',p_intent->>'memory_type','content',p_intent->>'content','metadata',p_intent->'metadata')
    WHEN 'delegation_request' THEN jsonb_build_object('action','delegate','to_agent',p_intent->>'to_agent','task',p_intent->'task','reason',p_intent->>'reason')
    WHEN 'failure' THEN jsonb_build_object('action','fail','reason',p_intent->>'reason')
  END;

  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (
    p_tenant_id,
    p_run_id,
    'intent_mapped_to_command',
    app.agent_intent_event_payload(p_intent) || jsonb_build_object('task_id', p_task_id, 'agent_id', p_agent_id, 'command_action', command->>'action'),
    p_actor
  );

  RETURN app.submit_agent_command(p_tenant_id, p_run_id, p_task_id, p_agent_id, command, p_actor);
END;
$$;
