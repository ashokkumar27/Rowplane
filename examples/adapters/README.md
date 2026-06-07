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
