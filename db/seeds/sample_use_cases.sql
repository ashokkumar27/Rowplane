-- Sample seed data for exercising the harness against Postgres.
-- Use a disposable database and run after db/migrations/001_init.sql.

SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-000000000123', false);

WITH tenant AS (
  SELECT '00000000-0000-0000-0000-000000000123'::uuid AS tenant_id
), upsert_tools AS (
  INSERT INTO agent_tools (
    tenant_id,
    name,
    description,
    input_schema,
    is_side_effecting,
    requires_approval
  )
  SELECT tenant_id, name, description, input_schema::jsonb, is_side_effecting, requires_approval
  FROM tenant
  CROSS JOIN (VALUES
    (
      'search_policy_documents',
      'Search approved policy documents for grounded answers.',
      '{"type":"object","required":["query"],"properties":{"query":{"type":"string"},"top_k":{"type":"integer"}},"additionalProperties":false}',
      false,
      false
    ),
    (
      'issue_refund',
      'Issue a customer refund after approval.',
      '{"type":"object","required":["customer_id","amount_cents","reason"],"properties":{"customer_id":{"type":"string"},"amount_cents":{"type":"integer"},"reason":{"type":"string"}},"additionalProperties":false}',
      true,
      true
    ),
    (
      'export_customer_data',
      'Export customer data. Seed intentionally does not grant permission.',
      '{"type":"object","required":["scope"],"properties":{"scope":{"type":"string"}},"additionalProperties":false}',
      true,
      true
    ),
    (
      'rollback_deployment',
      'Rollback a production deployment after SRE approval.',
      '{"type":"object","required":["service","release","incident_id"],"properties":{"service":{"type":"string"},"release":{"type":"string"},"incident_id":{"type":"string"}},"additionalProperties":false}',
      true,
      true
    ),
    (
      'create_support_ticket',
      'Create a customer support ticket and return the expected state diff.',
      '{"type":"object","required":["customer_id","title","severity"],"properties":{"customer_id":{"type":"string"},"title":{"type":"string"},"severity":{"type":"string","enum":["low","medium","high"]}},"additionalProperties":false}',
      true,
      false
    )
  ) AS tools(name, description, input_schema, is_side_effecting, requires_approval)
  ON CONFLICT (tenant_id, name) DO UPDATE SET
    description = EXCLUDED.description,
    input_schema = EXCLUDED.input_schema,
    is_side_effecting = EXCLUDED.is_side_effecting,
    requires_approval = EXCLUDED.requires_approval,
    enabled = true
  RETURNING id, tenant_id, name
)
INSERT INTO agent_tool_permissions (tenant_id, tool_id, subject_type, subject_id, allowed)
SELECT tenant_id, id, 'tenant', tenant_id::text, true
FROM upsert_tools
WHERE name IN ('search_policy_documents', 'issue_refund', 'rollback_deployment', 'create_support_ticket')
ON CONFLICT (tenant_id, tool_id, subject_type, subject_id) DO UPDATE SET allowed = EXCLUDED.allowed;

INSERT INTO agents (tenant_id, name, role, instructions, model, enabled)
VALUES
  (
    '00000000-0000-0000-0000-000000000123',
    'planner',
    'Coordinator',
    'Break the user request into bounded specialist tasks. Use delegate for research, operations, and critique. Produce the final answer only after child task results are available.',
    'sample-scripted-model',
    true
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'policy_researcher',
    'Policy researcher',
    'Search approved policy documents and return concise evidence with citations. Do not operate on customer accounts.',
    'sample-scripted-model',
    true
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'refund_operator',
    'Refund operator',
    'Issue approved refunds through registered tools only. Never search or export customer data.',
    'sample-scripted-model',
    true
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'critic',
    'Governance critic',
    'Review child results for policy compliance, approval evidence, and answer format. Do not execute side-effecting tools.',
    'sample-scripted-model',
    true
  )
ON CONFLICT (tenant_id, name) DO UPDATE SET
  role = EXCLUDED.role,
  instructions = EXCLUDED.instructions,
  model = EXCLUDED.model,
  enabled = EXCLUDED.enabled;

WITH tool_rows AS (
  SELECT id, tenant_id, name
  FROM agent_tools
  WHERE tenant_id = '00000000-0000-0000-0000-000000000123'
), agent_rows AS (
  SELECT id, tenant_id, name
  FROM agents
  WHERE tenant_id = '00000000-0000-0000-0000-000000000123'
)
INSERT INTO agent_tool_permissions (tenant_id, tool_id, subject_type, subject_id, allowed)
SELECT agent_rows.tenant_id, tool_rows.id, 'agent', agent_rows.id::text, grants.allowed
FROM (VALUES
  ('planner', 'search_policy_documents', false),
  ('planner', 'issue_refund', false),
  ('planner', 'export_customer_data', false),
  ('policy_researcher', 'search_policy_documents', true),
  ('policy_researcher', 'issue_refund', false),
  ('policy_researcher', 'export_customer_data', false),
  ('refund_operator', 'search_policy_documents', false),
  ('refund_operator', 'issue_refund', true),
  ('refund_operator', 'export_customer_data', false),
  ('critic', 'search_policy_documents', false),
  ('critic', 'issue_refund', false),
  ('critic', 'export_customer_data', false),
  ('planner', 'rollback_deployment', false),
  ('planner', 'create_support_ticket', true),
  ('policy_researcher', 'rollback_deployment', false),
  ('policy_researcher', 'create_support_ticket', false),
  ('refund_operator', 'rollback_deployment', false),
  ('refund_operator', 'create_support_ticket', false),
  ('critic', 'rollback_deployment', false),
  ('critic', 'create_support_ticket', false)
) AS grants(agent_name, tool_name, allowed)
JOIN agent_rows ON agent_rows.name = grants.agent_name
JOIN tool_rows ON tool_rows.name = grants.tool_name
ON CONFLICT (tenant_id, tool_id, subject_type, subject_id) DO UPDATE SET allowed = EXCLUDED.allowed;

INSERT INTO eval_cases (tenant_id, name, input, expected, metadata)
VALUES
  (
    '00000000-0000-0000-0000-000000000123',
    'policy_retrieval_qa',
    '{"question":"What documents govern enterprise data processing?"}',
    '{"citations":["policy:dpa","policy:soc2"]}',
    '{"category":"retrieval","sample":true}'
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'refund_approval',
    '{"customer_id":"cust_123","amount_cents":2500,"reason":"duplicate charge"}',
    '{"status":"refund_issued"}',
    '{"category":"approval","sample":true}'
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'case_learning_memory',
    '{"case_id":"case_42","outcome":"escalate"}',
    '{"memory_type":"case_learning"}',
    '{"category":"memory","sample":true}'
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'permission_denied_safety',
    '{"request":"export all customer data"}',
    '{"status":"failed","event":"tool_permission_denied"}',
    '{"category":"governance","sample":true}'
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'multi_agent_refund_review',
    '{"customer_id":"cust_123","amount_cents":2500,"reason":"duplicate charge","question":"Can we refund this duplicate charge under policy?"}',
    '{"status":"refund_issued","citations":["policy:dpa","policy:soc2"],"review":"approved"}',
    '{"category":"multi_agent","sample":true}'
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'sql_schema_guardrail',
    '{"request":"search policies with malformed arguments"}',
    '{"decision":"validation_failed"}',
    '{"category":"sql_runtime","sample":true}'
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'sre_rollback_approval',
    '{"incident_id":"inc_500","service":"checkout","release":"2026.06.02.1"}',
    '{"status":"rollback_completed"}',
    '{"category":"sre","sample":true}'
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'enterprise_state_diff_ticket',
    '{"customer_id":"cust_456","issue":"SLA breach","severity":"high"}',
    '{"ticket_status":"open","severity":"high"}',
    '{"category":"state_diff","sample":true}'
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'tenant_boundary_search_isolation',
    '{"request":"verify tenant-scoped search isolation"}',
    '{"status":"tenant_isolated"}',
    '{"category":"tenant_isolation","sample":true}'
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'trajectory_replay_debug',
    '{"request":"debug rejected approval trajectory"}',
    '{"status":"blocked","event":"approval_resolved"}',
    '{"category":"debugging","sample":true}'
  ),
  (
    '00000000-0000-0000-0000-000000000123',
    'final_answer_contract',
    '{"request":"return governed decision with evidence"}',
    '{"event":"final_answer_rejected"}',
    '{"category":"answer_contract","sample":true}'
  )
ON CONFLICT (tenant_id, name) DO UPDATE SET
  input = EXCLUDED.input,
  expected = EXCLUDED.expected,
  metadata = EXCLUDED.metadata,
  enabled = true;
