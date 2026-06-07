-- SQL-native fan-out/fan-in dependencies for multi-agent task graphs.
-- Parent tasks wait on explicit dependency rows; Postgres decides when they can resume.

CREATE TABLE IF NOT EXISTS agent_task_dependencies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  parent_task_id uuid NOT NULL,
  child_task_id uuid NOT NULL,
  dependency_type text NOT NULL DEFAULT 'completion' CHECK (length(dependency_type) > 0),
  required boolean NOT NULL DEFAULT true,
  status text NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'satisfied', 'failed')),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, parent_task_id, child_task_id, dependency_type),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, parent_task_id) REFERENCES agent_tasks (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, child_task_id) REFERENCES agent_tasks (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_task_dependencies_parent
  ON agent_task_dependencies (tenant_id, parent_task_id, status, required);

CREATE INDEX IF NOT EXISTS idx_agent_task_dependencies_child
  ON agent_task_dependencies (tenant_id, child_task_id, status);

DROP TRIGGER IF EXISTS trg_agent_task_dependencies_updated_at ON agent_task_dependencies;
CREATE TRIGGER trg_agent_task_dependencies_updated_at
  BEFORE UPDATE ON agent_task_dependencies
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE agent_task_dependencies ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_task_dependencies FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_task_dependencies_tenant_isolation ON agent_task_dependencies;
CREATE POLICY agent_task_dependencies_tenant_isolation ON agent_task_dependencies
  USING (tenant_id = app.current_tenant_id())
  WITH CHECK (tenant_id = app.current_tenant_id());

CREATE OR REPLACE FUNCTION app.create_task_dependency(
  p_tenant_id uuid,
  p_run_id uuid,
  p_parent_task_id uuid,
  p_child_task_id uuid,
  p_dependency_type text DEFAULT 'completion',
  p_required boolean DEFAULT true,
  p_metadata jsonb DEFAULT '{}'::jsonb,
  p_actor text DEFAULT 'worker'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  dependency_row agent_task_dependencies%ROWTYPE;
BEGIN
  IF p_parent_task_id IS NULL OR p_child_task_id IS NULL THEN
    RAISE EXCEPTION 'parent_task_id and child_task_id are required' USING ERRCODE = '22023';
  END IF;
  IF p_parent_task_id = p_child_task_id THEN
    RAISE EXCEPTION 'task dependency cannot point to itself' USING ERRCODE = '22023';
  END IF;

  INSERT INTO agent_task_dependencies (
    tenant_id,
    run_id,
    parent_task_id,
    child_task_id,
    dependency_type,
    required,
    metadata
  )
  VALUES (
    p_tenant_id,
    p_run_id,
    p_parent_task_id,
    p_child_task_id,
    COALESCE(NULLIF(p_dependency_type, ''), 'completion'),
    COALESCE(p_required, true),
    COALESCE(p_metadata, '{}'::jsonb)
  )
  ON CONFLICT (tenant_id, parent_task_id, child_task_id, dependency_type)
  DO UPDATE SET
    required = EXCLUDED.required,
    metadata = agent_task_dependencies.metadata || EXCLUDED.metadata
  RETURNING * INTO dependency_row;

  INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
  VALUES (
    dependency_row.tenant_id,
    dependency_row.run_id,
    'task_dependency_created',
    jsonb_build_object(
      'dependency_id', dependency_row.id,
      'parent_task_id', dependency_row.parent_task_id,
      'child_task_id', dependency_row.child_task_id,
      'dependency_type', dependency_row.dependency_type,
      'required', dependency_row.required
    ),
    p_actor
  );

  RETURN jsonb_build_object(
    'decision', 'created',
    'dependency_id', dependency_row.id,
    'parent_task_id', dependency_row.parent_task_id,
    'child_task_id', dependency_row.child_task_id,
    'status', dependency_row.status
  );
END;
$$;

CREATE OR REPLACE FUNCTION app.complete_task_dependencies_for_child(
  p_tenant_id uuid,
  p_run_id uuid,
  p_child_task_id uuid,
  p_child_status text,
  p_actor text DEFAULT 'worker'
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  dependency_row agent_task_dependencies%ROWTYPE;
  parent_row agent_tasks%ROWTYPE;
  next_dependency_status text;
  updated_count integer := 0;
  released_count integer := 0;
  blocked_count integer := 0;
  parent_id uuid;
BEGIN
  next_dependency_status := CASE
    WHEN p_child_status = 'completed' THEN 'satisfied'
    ELSE 'failed'
  END;

  FOR dependency_row IN
    UPDATE agent_task_dependencies
    SET status = next_dependency_status,
        resolved_at = COALESCE(resolved_at, now()),
        metadata = metadata || jsonb_build_object('child_status', p_child_status)
    WHERE tenant_id = p_tenant_id
      AND run_id = p_run_id
      AND child_task_id = p_child_task_id
      AND status = 'waiting'
    RETURNING *
  LOOP
    updated_count := updated_count + 1;
    INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
    VALUES (
      dependency_row.tenant_id,
      dependency_row.run_id,
      CASE WHEN dependency_row.status = 'satisfied' THEN 'task_dependency_satisfied' ELSE 'task_dependency_failed' END,
      jsonb_build_object(
        'dependency_id', dependency_row.id,
        'parent_task_id', dependency_row.parent_task_id,
        'child_task_id', dependency_row.child_task_id,
        'child_status', p_child_status,
        'required', dependency_row.required
      ),
      p_actor
    );
  END LOOP;

  FOR parent_id IN
    SELECT DISTINCT parent_task_id
    FROM agent_task_dependencies
    WHERE tenant_id = p_tenant_id
      AND run_id = p_run_id
      AND child_task_id = p_child_task_id
  LOOP
    SELECT * INTO parent_row
    FROM agent_tasks
    WHERE tenant_id = p_tenant_id AND id = parent_id
    FOR UPDATE;

    IF NOT FOUND THEN
      CONTINUE;
    END IF;

    IF EXISTS (
      SELECT 1
      FROM agent_task_dependencies d
      WHERE d.tenant_id = p_tenant_id
        AND d.run_id = p_run_id
        AND d.parent_task_id = parent_id
        AND d.required = true
        AND d.status = 'failed'
    ) THEN
      UPDATE agent_tasks
      SET status = 'blocked',
          error = 'required child task dependency failed'
      WHERE tenant_id = p_tenant_id
        AND id = parent_id
        AND status = 'waiting_child'
      RETURNING * INTO parent_row;
      IF FOUND THEN
        blocked_count := blocked_count + 1;
        INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
        VALUES (
          p_tenant_id,
          p_run_id,
          'task_dependency_parent_blocked',
          jsonb_build_object('parent_task_id', parent_id, 'child_task_id', p_child_task_id),
          p_actor
        );
      END IF;
    ELSIF NOT EXISTS (
      SELECT 1
      FROM agent_task_dependencies d
      WHERE d.tenant_id = p_tenant_id
        AND d.run_id = p_run_id
        AND d.parent_task_id = parent_id
        AND d.required = true
        AND d.status = 'waiting'
    ) THEN
      UPDATE agent_tasks
      SET status = 'queued'
      WHERE tenant_id = p_tenant_id
        AND id = parent_id
        AND status = 'waiting_child'
      RETURNING * INTO parent_row;
      IF FOUND THEN
        released_count := released_count + 1;
        INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
        VALUES (
          p_tenant_id,
          p_run_id,
          'task_dependency_parent_released',
          jsonb_build_object('parent_task_id', parent_id, 'child_task_id', p_child_task_id),
          p_actor
        );
        PERFORM app.send_agent_wakeup(p_tenant_id, p_run_id, parent_id);
      END IF;
    END IF;
  END LOOP;

  RETURN jsonb_build_object(
    'decision', CASE WHEN updated_count = 0 THEN 'no_dependencies' ELSE 'updated' END,
    'updated_count', updated_count,
    'released_count', released_count,
    'blocked_count', blocked_count,
    'child_task_id', p_child_task_id,
    'child_status', p_child_status
  );
END;
$$;
