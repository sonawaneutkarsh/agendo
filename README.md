# Agendo

Deterministic row-level rollback engine for AI agents on Postgres 16.

Agendo sits between your AI agent and Postgres. Every agent write runs inside a transaction tagged with `SET LOCAL agendo.action_id = '<uuid>'`, captured at the row level by a generic PL/pgSQL trigger (`agendo_capture()`). If the write turns out wrong, one click (or API call) atomically reverts only the rows that action touched—without tearing down or re-seeding the database.

## Setup

1. **Configure environment variables**:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` if you want custom Postgres credentials or to set `GEMINI_API_KEY` for `--live-gemini`.

2. **Start Postgres 16** (loads `sql/01_agendo.sql` and `demo/seed.sql` into a named volume `agendo_pgdata`):
   ```bash
   docker compose up -d --wait
   ```

3. **Create a virtualenv and install Agendo in editable mode**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

## Environment Variables

- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`: Postgres container credentials (required by `docker-compose.yml`).
- `DATABASE_URL`: Postgres connection string (e.g. `postgresql://agendo:agendo_dev_password@localhost:5432/agendo`).
- `GEMINI_API_KEY`: Required only when running with `--live-gemini` (fails fast if unset).
- `GEMINI_FLASH_MODEL`: Flash model ID for diagnostic summaries (defaults to `gemini-3.8-flash`).
- `GEMINI_REASONING_MODEL`: Reasoning model ID for tool-called repair SQL (defaults to `gemini-4-hydrogen`).

## Attaching Agendo to Your Own Tables

Any table with a `PRIMARY KEY` can be tracked with one SQL call (or `agendo.attach_trigger("orders")` in Python):

```sql
SELECT attach_agendo_trigger('your_table_name');
```

## Run the Demo Scenario

1. Start the demo server (core Agendo API + ESP32 `/bench` router + Postgres-persisted serial push hook):
   ```bash
   uvicorn demo.app:app --reload --port 8000
   ```
   *(For the pure hardware-agnostic core API without `/bench`, run `uvicorn agendo.api:app --port 8000`.)*

2. Open [http://localhost:8000](http://localhost:8000) to see the healthy ESP32 test bench (`esp32-bench-01`).

3. Run the ESP32 brownout-diagnosis agent:
   ```bash
   # Deterministic offline demo:
   python -m demo.run_demo

   # Live Gemini Flash + Gemini 4 Hydrogen tool-calling (requires GEMINI_API_KEY):
   python -m demo.run_demo --live-gemini
   ```

4. Refresh [http://localhost:8000](http://localhost:8000), inspect the red/green JSON diff, and click **Revert** (or see [demo/script.md](demo/script.md)).

## Run the Acceptance Tests

```bash
pytest -v
```
