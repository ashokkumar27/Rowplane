CREATE TABLE IF NOT EXISTS audit_events (
  event_id bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL,
  event_type text NOT NULL CHECK (length(event_type) > 0),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  actor text NOT NULL DEFAULT 'management_api',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant
  ON audit_events (tenant_id, event_id DESC);

CREATE OR REPLACE FUNCTION app.prevent_audit_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_events_append_only_update ON audit_events;
CREATE TRIGGER trg_audit_events_append_only_update
  BEFORE UPDATE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION app.prevent_audit_event_mutation();

DROP TRIGGER IF EXISTS trg_audit_events_append_only_delete ON audit_events;
CREATE TRIGGER trg_audit_events_append_only_delete
  BEFORE DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION app.prevent_audit_event_mutation();

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_events_tenant_isolation ON audit_events;
CREATE POLICY audit_events_tenant_isolation ON audit_events
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

CREATE OR REPLACE VIEW management_run_summary_v
WITH (security_invoker = true)
AS
SELECT
  r.tenant_id,
  r.id AS run_id,
  r.status,
  r.model,
  r.eval_case_id,
  r.iteration_count,
  r.max_iterations,
  r.created_at,
  r.updated_at,
  r.completed_at,
  EXTRACT(EPOCH FROM (COALESCE(r.completed_at, now()) - r.created_at))::integer AS duration_seconds,
  COALESCE(task_counts.task_count, 0) AS task_count,
  COALESCE(task_counts.pending_task_count, 0) AS pending_task_count,
  COALESCE(approval_counts.pending_approval_count, 0) AS pending_approval_count,
  COALESCE(tool_counts.tool_execution_count, 0) AS tool_execution_count,
  last_event.event_type AS latest_event_type,
  last_event.created_at AS latest_event_at
FROM agent_runs r
LEFT JOIN LATERAL (
  SELECT
    count(*)::integer AS task_count,
    count(*) FILTER (WHERE status IN ('queued', 'thinking', 'needs_tool', 'tool_running', 'waiting_approval', 'waiting_child'))::integer AS pending_task_count
  FROM agent_tasks t
  WHERE t.tenant_id = r.tenant_id AND t.run_id = r.id
) task_counts ON true
LEFT JOIN LATERAL (
  SELECT count(*)::integer AS pending_approval_count
  FROM approval_requests a
  WHERE a.tenant_id = r.tenant_id AND a.run_id = r.id AND a.status = 'pending'
) approval_counts ON true
LEFT JOIN LATERAL (
  SELECT count(*)::integer AS tool_execution_count
  FROM tool_executions te
  WHERE te.tenant_id = r.tenant_id AND te.run_id = r.id
) tool_counts ON true
LEFT JOIN LATERAL (
  SELECT event_type, created_at
  FROM agent_events e
  WHERE e.tenant_id = r.tenant_id AND e.run_id = r.id
  ORDER BY event_id DESC
  LIMIT 1
) last_event ON true;

CREATE OR REPLACE VIEW management_approval_queue_v
WITH (security_invoker = true)
AS
SELECT
  a.id AS approval_request_id,
  a.tenant_id,
  a.run_id,
  a.task_id,
  a.tool_execution_id,
  a.status,
  a.reason,
  a.payload,
  a.requested_by,
  a.resolved_by,
  a.resolved_at,
  a.created_at,
  a.updated_at,
  r.status AS run_status,
  t.status AS task_status,
  tools.name AS tool_name,
  tools.is_side_effecting,
  tools.requires_approval,
  EXTRACT(EPOCH FROM (now() - a.created_at))::integer AS age_seconds
FROM approval_requests a
JOIN agent_runs r ON r.tenant_id = a.tenant_id AND r.id = a.run_id
LEFT JOIN agent_tasks t ON t.tenant_id = a.tenant_id AND t.id = a.task_id
LEFT JOIN tool_executions te ON te.tenant_id = a.tenant_id AND te.id = a.tool_execution_id
LEFT JOIN agent_tools tools ON tools.tenant_id = te.tenant_id AND tools.id = te.tool_id;

CREATE OR REPLACE VIEW management_tool_health_v
WITH (security_invoker = true)
AS
SELECT
  tools.tenant_id,
  tools.id AS tool_id,
  tools.name AS tool_name,
  tools.enabled,
  tools.is_side_effecting,
  tools.requires_approval,
  count(te.id)::integer AS execution_count,
  count(te.id) FILTER (WHERE te.status = 'completed')::integer AS completed_count,
  count(te.id) FILTER (WHERE te.status = 'failed')::integer AS failed_count,
  count(te.id) FILTER (WHERE te.status = 'waiting_approval')::integer AS waiting_approval_count,
  CASE
    WHEN count(te.id) = 0 THEN NULL
    ELSE (count(te.id) FILTER (WHERE te.status = 'failed'))::numeric / count(te.id)::numeric
  END AS failure_rate
FROM agent_tools tools
LEFT JOIN tool_executions te ON te.tenant_id = tools.tenant_id AND te.tool_id = tools.id
GROUP BY tools.tenant_id, tools.id, tools.name, tools.enabled, tools.is_side_effecting, tools.requires_approval;

CREATE OR REPLACE VIEW management_eval_summary_v
WITH (security_invoker = true)
AS
SELECT
  ec.tenant_id,
  ec.id AS eval_case_id,
  ec.name AS eval_case_name,
  count(er.id)::integer AS result_count,
  avg(er.correctness) AS avg_correctness,
  avg(er.tool_correctness) AS avg_tool_correctness,
  avg(er.retrieval_relevance) AS avg_retrieval_relevance,
  avg(er.format_compliance) AS avg_format_compliance,
  avg(er.policy_compliance) AS avg_policy_compliance,
  max(er.created_at) AS latest_result_at
FROM eval_cases ec
LEFT JOIN eval_results er ON er.tenant_id = ec.tenant_id AND er.eval_case_id = ec.id
GROUP BY ec.tenant_id, ec.id, ec.name;
