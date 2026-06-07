-- Keep Postgres schema validation aligned with the Python contract subset.

CREATE OR REPLACE FUNCTION app.jsonb_matches_schema(p_value jsonb, schema jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  schema_type text;
  required_key text;
  prop record;
  item jsonb;
  numeric_value numeric;
BEGIN
  IF schema IS NULL OR schema = '{}'::jsonb THEN
    RETURN jsonb_typeof(p_value) = 'object';
  END IF;

  IF schema ? 'const' AND p_value <> schema->'const' THEN
    RETURN false;
  END IF;

  IF jsonb_typeof(schema->'enum') = 'array' THEN
    IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(schema->'enum') AS enum_item(v) WHERE enum_item.v = p_value) THEN
      RETURN false;
    END IF;
  END IF;

  schema_type := schema->>'type';
  IF schema_type IS NOT NULL THEN
    IF schema_type = 'object' AND jsonb_typeof(p_value) <> 'object' THEN RETURN false; END IF;
    IF schema_type = 'array' AND jsonb_typeof(p_value) <> 'array' THEN RETURN false; END IF;
    IF schema_type = 'string' AND jsonb_typeof(p_value) <> 'string' THEN RETURN false; END IF;
    IF schema_type = 'number' AND jsonb_typeof(p_value) <> 'number' THEN RETURN false; END IF;
    IF schema_type = 'integer' THEN
      IF jsonb_typeof(p_value) <> 'number' THEN RETURN false; END IF;
      numeric_value := (p_value #>> '{}')::numeric;
      IF numeric_value <> trunc(numeric_value) THEN RETURN false; END IF;
    END IF;
    IF schema_type = 'boolean' AND jsonb_typeof(p_value) <> 'boolean' THEN RETURN false; END IF;
    IF schema_type = 'null' AND jsonb_typeof(p_value) <> 'null' THEN RETURN false; END IF;
  END IF;

  IF jsonb_typeof(p_value) = 'string' AND schema ? 'minLength' THEN
    IF length(p_value #>> '{}') < (schema->>'minLength')::integer THEN
      RETURN false;
    END IF;
  END IF;

  IF jsonb_typeof(p_value) = 'number' THEN
    numeric_value := (p_value #>> '{}')::numeric;
    IF schema ? 'minimum' AND numeric_value < (schema->>'minimum')::numeric THEN RETURN false; END IF;
    IF schema ? 'maximum' AND numeric_value > (schema->>'maximum')::numeric THEN RETURN false; END IF;
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
      IF p_value ? prop.key AND NOT app.jsonb_matches_schema(p_value->prop.key, prop.schema_value) THEN
        RETURN false;
      END IF;
    END LOOP;
  END IF;

  IF schema_type = 'array' OR schema ? 'items' THEN
    IF jsonb_typeof(p_value) <> 'array' THEN
      RETURN false;
    END IF;
    IF jsonb_typeof(schema->'items') = 'object' THEN
      FOR item IN SELECT value FROM jsonb_array_elements(p_value) LOOP
        IF NOT app.jsonb_matches_schema(item, schema->'items') THEN
          RETURN false;
        END IF;
      END LOOP;
    END IF;
  END IF;

  RETURN true;
EXCEPTION
  WHEN invalid_text_representation THEN
    RETURN false;
END;
$$;
