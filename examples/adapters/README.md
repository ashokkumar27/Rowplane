# Adapter Examples

These examples show how another agent system can propose the next action while Rowplane keeps Postgres as the control plane.

The important pattern is:

```text
framework agent -> one Rowplane command -> Postgres governance -> worker tools
```

Do not expose side-effecting Rowplane tools as directly executable framework tools unless you intentionally want to bypass Rowplane's approval, idempotency, and audit guarantees.

## OpenAI Agents Customer Support

```bash
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rowplane \
OPENAI_API_KEY=... \
  python -B examples/adapters/openai_agents_customer_support.py
```

The example defaults to `gpt-5.4-mini` and requires `pip install -e '.[openai-agents]'`.

## LangGraph And Deep Agents Intent Bridges

The LangGraph and Deep Agents adapters are planner-only intent wrappers:

```text
framework planner -> one Rowplane intent -> Rowplane decision -> internal command -> governed execution
```

Do not bind Rowplane tools into LangGraph `ToolNode` or Deep Agents tools. The wrappers reject framework-native tool calls and return only one `RowplaneIntent` per worker iteration. Rowplane decides permissions, schema validity, approvals, idempotency, event writes, and execution.
