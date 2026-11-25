"""Comprehensive acceptance & regression test suite for Agendo."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agendo.agent import run_diagnosis_and_repair
from agendo.api import app as core_app
from agendo.capture import attach_trigger, table_row_hash
from agendo.db import get_connection
from agendo.proxy import ActionIdCollisionError, PermissionDeniedError, execute_write
from agendo.restore import clear_post_restore_hooks, restore_action
from demo.app import app as demo_app
from demo.hardware_hook import esp32_post_restore_hook

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = (ROOT / "sql" / "01_agendo.sql").read_text()
SEED_SQL = (ROOT / "demo" / "seed.sql").read_text()


@pytest.fixture(autouse=True)
def clean_db():
    """Reset schema and re-seed before each test so tests are isolated and deterministic."""
    clear_post_restore_hooks()
    with get_connection() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('agendo.is_restoring', 'true', true)")
            conn.execute(
                """
                DROP TABLE IF EXISTS restrict_children, restrict_parents, custom_orders CASCADE;
                DROP TABLE IF EXISTS agendo_hardware_pushes, diag_logs, calibration, pin_configs, esp32_devices CASCADE;
                DROP TABLE IF EXISTS agendo_row_diffs, agendo_actions CASCADE;
                DROP SEQUENCE IF EXISTS agendo_diff_seq CASCADE;
                """
            )
            conn.execute(SCHEMA_SQL)
            conn.execute(SEED_SQL)
    yield
    clear_post_restore_hooks()


def test_1_exact_round_trip():
    """Agent runs INSERT, UPDATE, and DELETE across 2 FK-linked tables in one action;
    undo() restores exact per-table deterministic MD5 row hashes.
    """
    with get_connection() as conn:
        before_devices = table_row_hash("esp32_devices", "board_id", conn)
        before_pins = table_row_hash("pin_configs", "gpio_pin", conn)
        before_cal = table_row_hash("calibration", "sensor", conn)

        res = execute_write(
            sql=[
                (
                    "INSERT INTO esp32_devices (board_id, name, firmware_status) VALUES (%s, %s, %s)",
                    ("esp32-bench-02", "Secondary Bench", "testing"),
                ),
                (
                    "INSERT INTO pin_configs (board_id, gpio_pin, mode, voltage_mv, pull_up) VALUES (%s, %s, %s, %s, %s)",
                    ("esp32-bench-02", 2, "GPIO_OUT", 3300, False),
                ),
                (
                    "UPDATE pin_configs SET mode = %s, voltage_mv = %s WHERE board_id = %s AND gpio_pin = %s",
                    ("OUTPUT_HIGH", 3300, "esp32-bench-01", 12),
                ),
                (
                    "DELETE FROM calibration WHERE board_id = %s AND sensor = %s",
                    ("esp32-bench-01", "vbat_adc"),
                ),
            ],
            agent_name="test-agent",
            description="Multi-table INSERT + UPDATE + DELETE",
            conn=conn,
        )

        assert table_row_hash("esp32_devices", "board_id", conn) != before_devices
        assert table_row_hash("pin_configs", "gpio_pin", conn) != before_pins
        assert table_row_hash("calibration", "sensor", conn) != before_cal

        undo_res = restore_action(res["action_id"], conn=conn)
        assert undo_res["status"] == "reverted"
        assert "REVERT NOTICE" in undo_res["revert_notice"]

        assert table_row_hash("esp32_devices", "board_id", conn) == before_devices
        assert table_row_hash("pin_configs", "gpio_pin", conn) == before_pins
        assert table_row_hash("calibration", "sensor", conn) == before_cal


def test_2_reverse_order_fk_safety():
    """Agent deletes parent + child (and separately inserts parent + child);
    undo() restores both directions with zero FK errors.
    """
    with get_connection() as conn:
        before_devices = table_row_hash("esp32_devices", "board_id", conn)
        before_pins = table_row_hash("pin_configs", "gpio_pin", conn)

        # Direction A: INSERT parent then child -> undo must delete child before parent
        ins_res = execute_write(
            sql=[
                (
                    "INSERT INTO esp32_devices (board_id, name, firmware_status) VALUES (%s, %s, %s)",
                    ("esp32-fk-test", "FK Board", "healthy"),
                ),
                (
                    "INSERT INTO pin_configs (board_id, gpio_pin, mode, voltage_mv, pull_up) VALUES (%s, %s, %s, %s, %s)",
                    ("esp32-fk-test", 14, "ADC_IN", 1800, False),
                ),
            ],
            agent_name="fk-agent",
            description="Insert parent + child",
            conn=conn,
        )
        undo_ins = restore_action(ins_res["action_id"], conn=conn)
        assert undo_ins["status"] == "reverted"
        assert table_row_hash("esp32_devices", "board_id", conn) == before_devices
        assert table_row_hash("pin_configs", "gpio_pin", conn) == before_pins

        # Direction B: DELETE child then parent -> undo must restore parent before child
        del_res = execute_write(
            sql=[
                ("DELETE FROM calibration WHERE board_id = %s", ("esp32-bench-01",)),
                ("DELETE FROM diag_logs WHERE board_id = %s", ("esp32-bench-01",)),
                ("DELETE FROM pin_configs WHERE board_id = %s", ("esp32-bench-01",)),
                ("DELETE FROM esp32_devices WHERE board_id = %s", ("esp32-bench-01",)),
            ],
            agent_name="fk-agent",
            description="Delete children + parent",
            conn=conn,
        )
        undo_del = restore_action(del_res["action_id"], conn=conn)
        assert undo_del["status"] == "reverted"
        assert table_row_hash("esp32_devices", "board_id", conn) == before_devices
        assert table_row_hash("pin_configs", "gpio_pin", conn) == before_pins


def test_3_conflict_guard():
    """Agent updates row X; external session updates row X; undo() refuses with 409 + 3-way diff."""
    res = execute_write(
        sql="UPDATE pin_configs SET voltage_mv = %s WHERE board_id = %s AND gpio_pin = %s",
        params=(2500, "esp32-bench-01", 12),
        agent_name="conflict-agent",
        description="Agent sets GPIO12 to 2500mV",
    )

    with get_connection() as raw_conn:
        with raw_conn.transaction():
            raw_conn.execute(
                "UPDATE pin_configs SET voltage_mv = 3000 WHERE board_id = 'esp32-bench-01' AND gpio_pin = 12"
            )

    client = TestClient(core_app)
    resp = client.post(f"/actions/{res['action_id']}/undo")
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["three_way_diff"]["old"]["voltage_mv"] == 1800
    assert body["three_way_diff"]["agent_new"]["voltage_mv"] == 2500
    assert body["three_way_diff"]["current"]["voltage_mv"] == 3000


def test_4_idempotency():
    """Calling undo twice on the same action_id is a clean no-op (already_reverted, HTTP 200)."""
    res = execute_write(
        sql="UPDATE pin_configs SET pull_up = false WHERE board_id = 'esp32-bench-01' AND gpio_pin = 4",
        agent_name="idempotent-agent",
        description="Disable GPIO4 pull-up",
    )

    client = TestClient(core_app)
    first = client.post(f"/actions/{res['action_id']}/undo")
    assert first.status_code == 200
    assert first.json()["status"] == "reverted"

    second = client.post(f"/actions/{res['action_id']}/undo")
    assert second.status_code == 200
    assert second.json()["status"] == "already_reverted"
    assert second.json()["restored_rows"] == []


def test_5_bypass_detection():
    """Raw psql write (no SDK, no action_id) is still captured with action_id NULL and flagged untracked."""
    with get_connection() as raw_conn:
        with raw_conn.transaction():
            raw_conn.execute(
                "UPDATE pin_configs SET voltage_mv = 1111 WHERE board_id = 'esp32-bench-01' AND gpio_pin = 13"
            )

    client = TestClient(core_app)
    actions = client.get("/actions").json()
    assert actions[0]["action_id"] == "untracked"
    assert actions[0]["status"] == "untracked"
    assert "UNTRACKED WRITE DETECTED" in actions[0]["description"]

    detail = client.get("/actions/untracked").json()
    assert len(detail["diffs"]) == 1
    assert detail["diffs"][0]["action_id"] is None
    assert detail["diffs"][0]["new_row"]["voltage_mv"] == 1111


def test_6_no_silent_writes_and_atomic_undo():
    """After demo scenario, every mutation has a snapshot row; mid-undo failure leaves DB intact."""
    with get_connection() as conn:
        before_pins = table_row_hash("pin_configs", "gpio_pin", conn)
        before_cal = table_row_hash("calibration", "sensor", conn)

    outcome = run_diagnosis_and_repair(use_live_gemini=False)
    action_id = outcome["action"]["action_id"]
    assert outcome["action"]["diffs_captured"] == 6

    with get_connection() as conn:
        after_bad_pins = table_row_hash("pin_configs", "gpio_pin", conn)
        after_bad_cal = table_row_hash("calibration", "sensor", conn)
        assert after_bad_pins != before_pins
        assert after_bad_cal != before_cal

        with pytest.raises(RuntimeError, match="Injected mid-undo failure"):
            restore_action(action_id, conn=conn, _inject_failure_after=2)

        assert table_row_hash("pin_configs", "gpio_pin", conn) == after_bad_pins
        assert table_row_hash("calibration", "sensor", conn) == after_bad_cal

        undo_res = restore_action(action_id, conn=conn)
        assert undo_res["status"] == "reverted"
        assert len(undo_res["restored_rows"]) == 6
        assert table_row_hash("pin_configs", "gpio_pin", conn) == before_pins
        assert table_row_hash("calibration", "sensor", conn) == before_cal


def test_sqlparse_permission_denylist_blocks_bypasses_and_allows_literals():
    """sqlparse blocks DDL, DO $$ blocks, CREATE/REPLACE, schema-qualified tables,
    while allowing string literals like 'ALTER ego'.
    """
    for bad_sql in (
        "DROP TABLE pin_configs",
        "ALTER TABLE pin_configs ADD COLUMN foo TEXT",
        "TRUNCATE TABLE pin_configs",
        "CREATE OR REPLACE FUNCTION evil() RETURNS VOID AS $$ BEGIN END; $$ LANGUAGE plpgsql",
        "DO $$ BEGIN DELETE FROM pin_configs; END $$",
        "DELETE FROM public.agendo_actions",
        'UPDATE "agendo_row_diffs" SET table_name = \'x\'',
        "SELECT * FROM pg_catalog.pg_tables",
    ):
        with pytest.raises(PermissionDeniedError):
            execute_write(bad_sql)

    # String literal 'ALTER ego' must NOT trigger a false positive
    res = execute_write(
        sql="INSERT INTO diag_logs (board_id, message) VALUES (%s, %s)",
        params=("esp32-bench-01", "ALTER ego DROP table false positive check"),
        agent_name="literal-test",
        description="Safe string literal insert",
    )
    assert res["diffs_captured"] == 1


def test_action_id_collision_is_rejected():
    """Explicitly supplying an existing action_id fails loudly unless allow_append=True."""
    aid = str(uuid.uuid4())
    execute_write(
        sql="INSERT INTO diag_logs (board_id, message) VALUES (%s, %s)",
        params=("esp32-bench-01", "first write"),
        action_id=aid,
        agent_name="turn-agent",
        description="turn 1",
    )
    with pytest.raises(ActionIdCollisionError):
        execute_write(
            sql="INSERT INTO diag_logs (board_id, message) VALUES (%s, %s)",
            params=("esp32-bench-01", "colliding write"),
            action_id=aid,
            agent_name="turn-agent",
            description="collision",
        )


def test_api_bad_uuid_returns_422_and_pagination_works():
    """Malformed UUIDs return 422 JSON (not 500) and GET /actions supports limit/offset."""
    client = TestClient(core_app)

    bad_get = client.get("/actions/not-a-uuid")
    assert bad_get.status_code == 422

    bad_post = client.post("/actions/not-a-uuid/undo")
    assert bad_post.status_code == 422

    for i in range(3):
        execute_write(
            sql="INSERT INTO diag_logs (board_id, message) VALUES (%s, %s)",
            params=("esp32-bench-01", f"msg {i}"),
            agent_name=f"agent-{i}",
            description=f"desc {i}",
        )

    page1 = client.get("/actions?limit=2&offset=0")
    assert page1.status_code == 200
    assert page1.headers["X-Total-Count"] == "3"
    assert len(page1.json()) == 2

    page2 = client.get("/actions?limit=2&offset=2")
    assert page2.status_code == 200
    assert len(page2.json()) == 1


def test_foreign_key_violation_on_undo_returns_409():
    """External child row inserted on RESTRICT FK after parent insert triggers 409, not 500."""
    with get_connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                CREATE TABLE restrict_parents (id INT PRIMARY KEY, name TEXT);
                CREATE TABLE restrict_children (
                    id INT PRIMARY KEY,
                    parent_id INT NOT NULL REFERENCES restrict_parents(id) ON DELETE RESTRICT
                );
                """
            )
        attach_trigger("restrict_parents", conn=conn)
        attach_trigger("restrict_children", conn=conn)

    res = execute_write(
        sql="INSERT INTO restrict_parents (id, name) VALUES (%s, %s)",
        params=(10, "parent-10"),
        agent_name="fk-restrict-agent",
        description="Insert parent 10",
    )

    # External session inserts a child referencing parent 10
    with get_connection() as raw_conn:
        with raw_conn.transaction():
            raw_conn.execute("INSERT INTO restrict_children (id, parent_id) VALUES (100, 10)")

    client = TestClient(core_app)
    resp = client.post(f"/actions/{res['action_id']}/undo")
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "conflict"
    assert "ForeignKeyViolation" in body["reason"]


def test_schema_evolution_delete_undo_handles_dropped_and_added_columns():
    """DELETE undo succeeds even if a column was dropped and a NOT NULL DEFAULT column was added."""
    res = execute_write(
        sql="DELETE FROM calibration WHERE board_id = %s AND sensor = %s",
        params=("esp32-bench-01", "temp_ntc"),
        agent_name="schema-drift-agent",
        description="Delete temp_ntc before schema drift",
    )

    with get_connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                ALTER TABLE calibration
                    DROP COLUMN offset_mv,
                    ADD COLUMN unit TEXT NOT NULL DEFAULT 'celsius';
                """
            )

    undo_res = restore_action(res["action_id"])
    assert undo_res["status"] == "reverted"

    with get_connection() as conn:
        row = conn.execute(
            "SELECT board_id, sensor, unit FROM calibration WHERE board_id = 'esp32-bench-01' AND sensor = 'temp_ntc'"
        ).fetchone()
        assert row == {"board_id": "esp32-bench-01", "sensor": "temp_ntc", "unit": "celsius"}


def test_core_vs_demo_separation_and_persisted_hardware_pushes():
    """Core API has no /bench route; demo API persists hardware pushes to Postgres."""
    core_client = TestClient(core_app)
    assert core_client.get("/bench").status_code == 404
    assert core_client.get("/").status_code == 200
    assert core_client.get("/static/styles.css").status_code == 200

    outcome = run_diagnosis_and_repair(use_live_gemini=False)
    action_id = outcome["action"]["action_id"]

    # demo_app registers esp32_post_restore_hook when imported; register explicitly for clarity
    demo_client = TestClient(demo_app)
    from agendo.restore import register_post_restore_hook

    register_post_restore_hook(esp32_post_restore_hook)
    resp = demo_client.post(f"/actions/{action_id}/undo")
    assert resp.status_code == 200
    assert len(resp.json()["hook_results"]) == 1

    bench = demo_client.get("/bench").json()
    assert bench["health"]["overall"] == "healthy"
    assert len(bench["hardware_push_log"]) == 1
    assert bench["hardware_push_log"][0]["action_id"] == action_id


def test_agent_live_fails_without_key_executes_model_sql_and_respects_revert_notice(monkeypatch):
    """Proves: (1) live mode fails fast without GEMINI_API_KEY, (2) client uses v1beta,
    (3) model-produced SQL is executed (not canned SQL) and fails loudly if missing,
    (4) revert notice stops re-applying bad writes.
    """
    from agendo.agent import _get_genai_client

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is not set"):
        run_diagnosis_and_repair(use_live_gemini=True)

    monkeypatch.setenv("GEMINI_API_KEY", "test-only-dummy-key")
    configured_client = _get_genai_client()
    assert configured_client._api_client._http_options.api_version == "v1beta"

    # Fake google-genai client simulating Flash diagnosis (config=None) + tool-calling step
    class FakeModels:
        def __init__(self, mode: str) -> None:
            self.mode = mode
            self.calls: list[str] = []

        def generate_content(self, model: str, contents: str, config=None):
            self.calls.append(model)
            if config is None:
                return SimpleNamespace(text="Flash saw GPIO12 at 1800mV.")
            if self.mode == "no_tool_call":
                return SimpleNamespace(text="I think you should inspect GPIO12.", function_calls=[])
            if "REVERT NOTICE:" in contents:
                return SimpleNamespace(
                    text="",
                    function_calls=[
                        SimpleNamespace(
                            name="skip_repair",
                            args={"reason": "Previous repair was reverted; skipping DB mutations."},
                        )
                    ],
                )
            return SimpleNamespace(
                text="",
                function_calls=[
                    SimpleNamespace(
                        name="apply_sql_repair",
                        args={
                            "description": "Model-decided log-only check",
                            "statements": [
                                {
                                    "sql": "INSERT INTO diag_logs (board_id, message) VALUES (%s, %s)",
                                    "params": ["esp32-bench-01", "Model-generated diagnostic entry"],
                                }
                            ],
                        },
                    )
                ],
            )

    # 1. Fail loudly if model produces no tool call / no SQL
    with pytest.raises(RuntimeError, match="produced no valid tool call"):
        run_diagnosis_and_repair(
            use_live_gemini=True,
            genai_client=SimpleNamespace(models=FakeModels("no_tool_call")),
        )

    # 2. Execute model-produced SQL (NOT canned GPIO12/calibration SQL), revert, then re-run w/ notice
    fake_client = SimpleNamespace(models=FakeModels("normal"))
    outcome = run_diagnosis_and_repair(use_live_gemini=True, genai_client=fake_client)
    assert outcome["skipped"] is False
    assert outcome["action"]["diffs_captured"] == 1
    assert "Model-generated diagnostic entry" in str(outcome["statements_executed_by_model"])

    revert_res = restore_action(outcome["action"]["action_id"])
    assert revert_res["status"] == "reverted"

    followup = run_diagnosis_and_repair(
        use_live_gemini=True,
        revert_notice=revert_res["revert_notice"],
        genai_client=fake_client,
    )
    assert followup["skipped"] is True
    assert followup["action"] is None


def test_attach_agendo_trigger_on_custom_table():
    """Users can attach agendo_capture() to arbitrary tables with attach_agendo_trigger()."""
    with get_connection() as conn:
        with conn.transaction():
            conn.execute(
                "CREATE TABLE custom_orders (order_id INT PRIMARY KEY, total_cents INT NOT NULL)"
            )
        attached = attach_trigger("custom_orders", conn=conn)
        assert "custom_orders" in attached

        before = table_row_hash("custom_orders", "order_id", conn)
        res = execute_write(
            sql="INSERT INTO custom_orders (order_id, total_cents) VALUES (%s, %s)",
            params=(1, 4900),
            agent_name="orders-agent",
            description="Create order 1",
            conn=conn,
        )
        assert table_row_hash("custom_orders", "order_id", conn) != before
        restore_action(res["action_id"], conn=conn)
        assert table_row_hash("custom_orders", "order_id", conn) == before


def test_self_contained_demo_corrupt_repair_revert_skip_loop():
    """Proves demo.run_demo.corrupt_board() corrupts GPIO12, I2C pull-ups, and calibration,
    then the agent repairs it, restore_action reverts it, and feeding revert_notice skips re-do.
    """
    from demo.run_demo import corrupt_board

    corrupt_res = corrupt_board()
    assert corrupt_res["diffs_captured"] == 5

    with get_connection() as conn:
        gpio12 = conn.execute(
            "SELECT mode, voltage_mv FROM pin_configs WHERE board_id = 'esp32-bench-01' AND gpio_pin = 12"
        ).fetchone()
        pullups = conn.execute(
            "SELECT COUNT(*)::int AS cnt FROM pin_configs WHERE board_id = 'esp32-bench-01' AND gpio_pin IN (4, 5) AND pull_up"
        ).fetchone()
        cal_count = conn.execute(
            "SELECT COUNT(*)::int AS cnt FROM calibration WHERE board_id = 'esp32-bench-01'"
        ).fetchone()
        assert gpio12 == {"mode": "OUTPUT_HIGH", "voltage_mv": 3300}
        assert pullups["cnt"] == 0
        assert cal_count["cnt"] == 0

    repair_outcome = run_diagnosis_and_repair(use_live_gemini=False)
    assert repair_outcome["skipped"] is False
    assert repair_outcome["action"] is not None

    revert_res = restore_action(repair_outcome["action"]["action_id"])
    assert revert_res["status"] == "reverted"

    followup = run_diagnosis_and_repair(
        use_live_gemini=False,
        revert_notice=revert_res["revert_notice"],
    )
    assert followup["skipped"] is True
    assert followup["action"] is None
