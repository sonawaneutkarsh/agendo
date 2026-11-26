# Agendo

Row-level rollback for AI agent writes on Postgres 16.

Agendo sits between your agent and Postgres. Every write runs inside a tagged transaction, a row trigger records before/after JSONB snapshots, and one call reverts only the rows that action touched.

## Install

Requires Python 3.12+ and Postgres 16.

```bash
pip install agendo
```

## 5-Minute Quickstart

Start a plain Postgres 16 container (no demo seed data):

```bash
docker run -d --name agendo-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16-alpine
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
```

Run the quickstart from this repo:

```bash
python examples/quickstart.py
```

Or copy this script into your own project after `pip install agendo`:

```python
import agendo

with agendo.get_connection() as conn:
    agendo.install_schema(conn=conn)

    with conn.transaction():
        conn.execute("SELECT set_config('agendo.is_restoring', 'true', true)")
        conn.execute("DROP TABLE IF EXISTS orders")
        conn.execute(
            """
            CREATE TABLE orders (
                order_id INT PRIMARY KEY,
                customer TEXT NOT NULL,
                status TEXT NOT NULL,
                total_cents INT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO orders (order_id, customer, status, total_cents)
            VALUES (1, 'alice', 'paid', 4900), (2, 'bob', 'paid', 12500), (3, 'carol', 'pending', 3200)
            """
        )

    agendo.attach_trigger("orders", conn=conn)

    # Agent makes a bad write: wrong UPDATE plus DELETE in one action
    action = agendo.execute_write(
        sql=[
            ("UPDATE orders SET status = %s, total_cents = %s WHERE order_id = %s", ("cancelled", 0, 1)),
            ("DELETE FROM orders WHERE order_id = %s", (3,)),
        ],
        agent_name="billing-agent",
        description="Mistakenly cancel order 1 and delete order 3",
        conn=conn,
    )

    # Revert the action atomically
    revert = agendo.restore_action(action["action_id"], conn=conn)
    print(revert["status"], revert["revert_notice"])
```

Expected output from `python examples/quickstart.py`:

```text
Attached trigger to: public.orders (order_id)

Original state:
  order_id=1 customer=alice status=paid total_cents=4900
  order_id=2 customer=bob status=paid total_cents=12500
  order_id=3 customer=carol status=pending total_cents=3200

Recorded action 8455a708-d08e-4813-ae71-1424aecb577e (2 row diffs captured).

Corrupted state:
  order_id=1 customer=alice status=cancelled total_cents=0
  order_id=2 customer=bob status=paid total_cents=12500

Revert status: reverted (2.68 ms)
REVERT NOTICE: Action 8455a708-d08e-4813-ae71-1424aecb577e (Mistakenly cancel order 1 and delete order 3) was reverted.
Restored 2 row(s) to their exact prior state:
- orders pk={'order_id': 3} (undid DELETE); restored={'status': 'pending', 'customer': 'carol', 'order_id': 3, 'total_cents': 3200}
- orders pk={'order_id': 1} (undid UPDATE); restored={'status': 'paid', 'customer': 'alice', 'order_id': 1, 'total_cents': 4900}
Do NOT re-apply the reverted changes without re-evaluating the current database state.

Restored state:
  order_id=1 customer=alice status=paid total_cents=4900
  order_id=2 customer=bob status=paid total_cents=12500
  order_id=3 customer=carol status=pending total_cents=3200

Verified: restored rows match original state exactly.
```

To inspect actions and revert from a browser UI:

```bash
uvicorn agendo.api:app --port 8000
```

Open [http://localhost:8000](http://localhost:8000).

## How It Works

- **Trigger**: `agendo.install_schema()` creates `agendo_actions`, `agendo_row_diffs`, the `agendo_capture()` trigger function, and `attach_agendo_trigger()`. Calling `agendo.attach_trigger("orders")` attaches an `AFTER INSERT OR UPDATE OR DELETE` trigger using the table's primary key.
- **Action ID**: `agendo.execute_write()` parses SQL with `sqlparse` and blocks DDL (`DROP`, `ALTER`, `TRUNCATE`, `CREATE`, `DO`, `GRANT`, `REVOKE`, `COPY`, `VACUUM`), system schemas, and Agendo tables. It creates a row in `agendo_actions`, sets `SET LOCAL agendo.action_id` for the transaction, and runs the queries. The trigger logs `old_row` and `new_row` JSONB snapshots with a monotonic `seq`.
- **Revert**: `agendo.restore_action(action_id)` (or `POST /actions/{action_id}/undo`) reads diffs in reverse `seq` order so parent and child foreign keys restore cleanly. Inside one transaction, it sets `agendo.is_restoring = 'true'` so the undo is not re-captured, runs compensating `DELETE`, `INSERT`, or `UPDATE` statements, marks the action `reverted`, and returns a `revert_notice` string you can feed back to your agent. Reverting the same action twice returns `already_reverted` as a no-op.
- **409 conflict**: Before reverting a row, Agendo checks that the live row still matches `new_row` (or is still absent for a `DELETE`). If another writer touched the row afterward, or a foreign key constraint blocks deleting a parent row, the restore rolls back. The action is marked `conflict`, `ConflictError` is raised (HTTP `409`), and a three-way diff (`old`, `agent_new`, `current`) is returned.
- **Untracked writes**: If raw SQL mutates a watched table without `execute_write()`, `agendo.action_id` is empty. The trigger still records the row diff with `action_id = NULL`. `list_actions()` and the UI surface those changes at the top under `action_id = "untracked"`.

## SDK and API Reference

### Python SDK (`import agendo`)

All functions read `DATABASE_URL` from the environment (or a local `.env` file) when `conn` is omitted, or accept an existing `psycopg.Connection`. Use `agendo.get_connection()` if you share a connection across calls.

#### `install_schema(conn=None)`
Creates `agendo_diff_seq`, `agendo_actions`, `agendo_row_diffs`, `agendo_capture()`, and `attach_agendo_trigger()`. Safe to call repeatedly.

#### `attach_trigger(table_name, conn=None) -> str`
Attaches `agendo_capture()` to `table_name` using its primary key columns. Raises if the table has no primary key.

#### `execute_write(sql, params=None, action_id=None, agent_name="agent", description="", conn=None, allow_append=False) -> dict`
Runs one SQL string (with optional `params`) or a sequence of `(sql, params)` tuples inside one tracked transaction.

- **Returns**: `action_id`, `agent_name`, `description`, `status` (`"applied"`), `statements_executed`, `rows_affected`, `diffs_captured`.
- **Raises**: `PermissionDeniedError` for blocked DDL or denied tables/schemas; `ActionIdCollisionError` if `action_id` already exists and `allow_append=False`.

#### `restore_action(action_id, conn=None, post_restore_hook=None) -> dict`
Reverts all row diffs captured under `action_id` in one atomic transaction.

- **Returns**: `status` (`"reverted"` or `"already_reverted"`), `action_id`, `restored_rows`, `revert_notice`, `hook_results`, `elapsed_ms`.
- **Raises**: `ActionNotFoundError` if `action_id` does not exist; `ConflictError` (call `.to_dict()` for `three_way_diff`) if a row changed after the agent write or a foreign key blocks undo.

#### Inspection and hooks
- `list_actions(limit=50, offset=0, conn=None) -> list[dict]` returns actions newest-first, plus a synthetic `"untracked"` entry if untracked diffs exist.
- `get_action_detail(action_id, conn=None) -> dict | None` returns action metadata and ordered `diffs`.
- `register_post_restore_hook(hook)` and `clear_post_restore_hooks()` register callbacks `hook(conn, action_id, restored_rows)` that run inside the restore transaction after rows revert.

### HTTP API (`uvicorn agendo.api:app --port 8000`)

- `GET /`: Web UI showing the action timeline, row diffs, and Revert button.
- `GET /actions?limit=50&offset=0`: Paginated list of actions with `X-Total-Count` header.
- `GET /actions/{action_id}`: Action metadata and ordered `diffs` (`404` if missing, `422` for invalid UUID; accepts `"untracked"`).
- `POST /actions/{action_id}/undo`: Reverts the action. Returns `200` with the restore payload, `404` if missing, `409` JSON with `three_way_diff` on conflict, or `422` for invalid UUID.

## Honest Limits

- **Primary keys required**: `attach_trigger()` rejects tables without a `PRIMARY KEY`.
- **DML only**: DDL (`DROP`, `ALTER`, `TRUNCATE`, `CREATE`), `GRANT`/`REVOKE`, `COPY`, `VACUUM`, and `DO` blocks are blocked because row triggers cannot revert schema or privilege changes.
- **Whole-row conflict checks**: In v1, any change to a row after the agent write triggers a `409` conflict, even if a later query edited a different column on that same row.
- **Untracked writes are alert-only**: Raw SQL outside `execute_write()` is captured and flagged as `"untracked"`, not grouped into revertible actions.
- **No backfill**: Capture starts when `attach_trigger()` runs. Writes made before attaching are not logged.
- **Database scope only**: Postgres triggers cannot undo external side effects like HTTP calls or hardware changes. Use `post_restore_hook` to push restored state downstream.
- **Schema search path**: Watched tables should be on the active Postgres `search_path` (default `public`), because the trigger records `TG_TABLE_NAME` without a schema prefix.

## Example: ESP32 Bench

This repository also includes an example in `demo/` where an agent diagnoses an ESP32 pin and calibration database and a post-restore hook logs mock serial pushes. It is one example built on Agendo, not Agendo itself.

1. Configure `.env` and start Postgres 16 with `demo/seed.sql`:
   ```bash
   cp .env.example .env
   docker compose up -d --wait
   ```

2. Install editable dev dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. Start the demo server (core API plus ESP32 `/bench` endpoint):
   ```bash
   uvicorn demo.app:app --reload --port 8000
   ```

4. Run the demo loop (corrupts `GPIO12`, disables I2C pull-ups, wipes calibration, runs repair, reverts, and checks `REVERT NOTICE`):
   ```bash
   # Deterministic offline run:
   python -m demo.run_demo

   # Live Gemini run (requires GEMINI_API_KEY in .env):
   python -m demo.run_demo --live-gemini

   # Pause before revert so you can click Revert at http://localhost:8000:
   python -m demo.run_demo --no-revert
   ```

5. Run the test suite:
   ```bash
   pytest -v
   ```
