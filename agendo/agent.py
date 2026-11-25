"""ESP32 hardware-diagnosis Gemini agent wrapped with Agendo."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from agendo.db import get_connection
from agendo.proxy import execute_write

logger = logging.getLogger("agendo.agent")

FLASH_MODEL = os.environ.get("GEMINI_FLASH_MODEL", "gemini-3.8-flash")
REASONING_MODEL = os.environ.get("GEMINI_REASONING_MODEL", "gemini-4-hydrogen")

EXPECTED_HEALTHY_BASELINE = """Expected-healthy baseline for esp32-bench-01:
- GPIO4 (pin_configs): mode='I2C_SDA', voltage_mv=3300, pull_up=true
- GPIO5 (pin_configs): mode='I2C_SCL', voltage_mv=3300, pull_up=true
- GPIO12 (pin_configs): mode='ADC_IN', voltage_mv=1800, pull_up=false
- GPIO13 (pin_configs): mode='STATUS_LED', voltage_mv=3300, pull_up=false
- calibration: rows for BOTH sensors MUST exist:
  - sensor='temp_ntc', offset_mv=8 (+8mV)
  - sensor='vbat_adc', offset_mv=-12 (-12mV)"""


def _read_bench_snapshot() -> dict[str, Any]:
    with get_connection() as conn:
        devices = list(conn.execute("SELECT * FROM esp32_devices ORDER BY board_id").fetchall())
        pins = list(conn.execute("SELECT * FROM pin_configs ORDER BY board_id, gpio_pin").fetchall())
        calibration = list(conn.execute("SELECT * FROM calibration ORDER BY board_id, sensor").fetchall())
        logs = list(conn.execute("SELECT * FROM diag_logs ORDER BY id DESC LIMIT 5").fetchall())
    return {
        "devices": devices,
        "pins": pins,
        "calibration": calibration,
        "logs": logs,
    }


def _is_bench_corrupted(state: dict[str, Any]) -> bool:
    pins = state.get("pins") or []
    calibration = state.get("calibration") or []
    gpio12_bad = any(
        p["gpio_pin"] == 12 and (p["mode"] != "ADC_IN" or p["voltage_mv"] != 1800)
        for p in pins
    )
    i2c_bad = sum(1 for p in pins if p["gpio_pin"] in (4, 5) and p["pull_up"]) < 2
    cal_by_sensor = {c["sensor"]: c["offset_mv"] for c in calibration}
    cal_bad = cal_by_sensor.get("temp_ntc") != 8 or cal_by_sensor.get("vbat_adc") != -12
    return gpio12_bad or i2c_bad or cal_bad


def _build_repair_tools():
    from google.genai import types

    apply_sql_repair = types.FunctionDeclaration(
        name="apply_sql_repair",
        description=(
            "Execute a repair plan as parameterized PostgreSQL DML statements "
            "(UPDATE/INSERT/DELETE) wrapped by Agendo."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short human-readable summary of the database repair.",
                },
                "statements": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "sql": {
                                "type": "string",
                                "description": "Parameterized PostgreSQL DML statement using %s placeholders.",
                            },
                            "params": {
                                "type": "array",
                                "items": {
                                    "type": ["string", "integer", "number", "boolean", "null"]
                                },
                                "description": "Positional parameter values matching %s placeholders.",
                            },
                        },
                        "required": ["sql", "params"],
                    },
                },
            },
            "required": ["description", "statements"],
        },
    )

    skip_repair = types.FunctionDeclaration(
        name="skip_repair",
        description=(
            "Return an explicit SKIP verdict without modifying the database — required when "
            "a human reviewed and reverted the previous repair or when the bench matches baseline."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Explicit SKIP verdict explaining why no database writes are made.",
                }
            },
            "required": ["reason"],
        },
    )

    return types.Tool(function_declarations=[apply_sql_repair, skip_repair])


def _get_genai_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    from google import genai

    # Function calling with parameters_json_schema requires v1beta across models (incl. gemini-2.5-flash)
    return genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})


def run_diagnosis_and_repair(
    board_id: str = "esp32-bench-01",
    use_live_gemini: bool = True,
    revert_notice: str | None = None,
    genai_client: Any = None,
) -> dict[str, Any]:
    """Run Flash diagnosis + reasoning with tool calling, executing writes via Agendo.

    - When `use_live_gemini=True`: fails fast if `GEMINI_API_KEY` is missing (unless a
      test `genai_client` is injected), requires valid tool calls from the model, and
      never falls back to hardcoded SQL or Flash on error.
    - When `use_live_gemini=False`: runs deterministic offline mode; respects
      `revert_notice` by returning an explicit SKIP verdict.
    """
    state = _read_bench_snapshot()
    notice_block = f"\n\n{revert_notice}\n" if revert_notice else ""

    if use_live_gemini:
        from google.genai import types

        flash_model = os.environ.get("GEMINI_FLASH_MODEL", FLASH_MODEL)
        reasoning_model = os.environ.get("GEMINI_REASONING_MODEL", REASONING_MODEL)

        client = genai_client or _get_genai_client()

        diag_system_instruction = (
            "You are an embedded systems diagnostic loop (Gemini Flash).\n"
            f"{EXPECTED_HEALTHY_BASELINE}\n\n"
            "Instructions:\n"
            "1. Compare the live bench state strictly against the expected-healthy baseline above.\n"
            "2. List EVERY single deviation (wrong pin mode, wrong voltage_mv, disabled pull_up, "
            "and missing or wrong calibration rows for temp_ntc / vbat_adc) before any repair is planned.\n"
            "3. Never treat a deviation from the baseline (such as GPIO12 OUTPUT_HIGH or missing calibration rows) as valid."
        )
        if revert_notice:
            diag_prompt = (
                f"A human deliberately reviewed and reverted the repair for board '{board_id}'.\n"
                "Summarize the current state and note that a human revert override is active — "
                "do NOT recommend re-applying the reverted writes.\n"
                f"Bench state:\n{json.dumps(state, default=str)}{notice_block}"
            )
        else:
            diag_prompt = (
                f"Compare the live ESP32 bench state for board '{board_id}' against the expected-healthy "
                "baseline and list every deviation concisely:\n"
                f"Bench state:\n{json.dumps(state, default=str)}"
            )

        flash_resp = client.models.generate_content(
            model=flash_model,
            contents=diag_prompt,
            config=types.GenerateContentConfig(
                system_instruction=diag_system_instruction,
            ),
        )
        flash_summary = (flash_resp.text or "").strip()

        repair_tool = _build_repair_tools()
        system_instruction = (
            "You are a hardware-bench SQL repair agent operating behind Agendo on PostgreSQL 16.\n"
            f"{EXPECTED_HEALTHY_BASELINE}\n\n"
            "Tables:\n"
            "- esp32_devices (board_id TEXT PK, name TEXT, firmware_status TEXT)\n"
            "- pin_configs (board_id TEXT, gpio_pin INT, mode TEXT, voltage_mv INT, pull_up BOOLEAN, PK (board_id, gpio_pin))\n"
            "- calibration (board_id TEXT, sensor TEXT, offset_mv INT, PK (board_id, sensor))\n"
            "- diag_logs (id SERIAL PK, board_id TEXT, message TEXT, created_at TIMESTAMPTZ)\n\n"
            "Rules:\n"
            "- Emit valid PostgreSQL DML ONLY (UPDATE / INSERT / DELETE), never DDL (DROP/ALTER/TRUNCATE/CREATE).\n"
            "- Use `%s` positional placeholders ONLY (never `$1` or `?`) with matching `params` arrays.\n"
            "- When NO human revert override is present, fix EVERY deviation from the expected-healthy "
            "baseline in one `apply_sql_repair` call (restore GPIO12 ADC_IN @ 1800mV, enable GPIO4/GPIO5 pull_up=true, "
            "restore temp_ntc=+8mV and vbat_adc=-12mV calibration rows via INSERT ... ON CONFLICT DO UPDATE, "
            "and insert a diag_logs entry).\n"
            "- MANDATORY HUMAN OVERRIDE RULE: If the prompt states that a human deliberately reviewed and "
            "reverted the repair, do NOT re-apply the reverted writes; reply with an explicit SKIP verdict "
            "by calling `skip_repair`."
        )

        if revert_notice:
            plan_prompt = (
                "HUMAN REVIEW OVERRIDE:\n"
                "A human deliberately reviewed and reverted the repair; do NOT re-apply the reverted writes; "
                "reply with an explicit SKIP verdict by calling `skip_repair`.\n\n"
                f"Flash diagnostic summary:\n{flash_summary}\n\n"
                f"Current database state:\n{json.dumps(state, default=str)}"
                f"{notice_block}"
            )
        else:
            plan_prompt = (
                f"Flash diagnostic summary (list of deviations):\n{flash_summary}\n\n"
                f"Current database state:\n{json.dumps(state, default=str)}\n\n"
                "Call `apply_sql_repair` with parameterized PostgreSQL SQL (%s placeholders) fixing EVERY "
                "deviation from the expected-healthy baseline."
            )

        try:
            plan_resp = client.models.generate_content(
                model=reasoning_model,
                contents=plan_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[repair_tool],
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.ANY,
                            allowed_function_names=["apply_sql_repair", "skip_repair"],
                        )
                    ),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
        except Exception as exc:
            logger.warning(
                "Reasoning model %s failed (%s); not falling back silently.",
                reasoning_model,
                exc,
            )
            raise

        function_calls = getattr(plan_resp, "function_calls", None) or []
        if not function_calls:
            raise RuntimeError(
                f"Model {reasoning_model} produced no valid tool call (text={plan_resp.text!r})"
            )

        call = function_calls[0]
        call_name = getattr(call, "name", "")
        args = dict(getattr(call, "args", {}) or {})

        if call_name == "skip_repair":
            reason = str(args.get("reason") or "SKIP: human reverted prior repair.")
            return {
                "flash_summary": flash_summary,
                "reasoning_plan": f"SKIP: {reason}",
                "skipped": True,
                "skip_reason": reason,
                "action": None,
            }

        if call_name != "apply_sql_repair":
            raise RuntimeError(f"Unexpected tool call from model: {call_name!r}")

        raw_stmts = args.get("statements")
        if not isinstance(raw_stmts, list) or not raw_stmts:
            raise RuntimeError("Model produced no valid SQL statements in apply_sql_repair")

        sql_pairs: list[tuple[str, list[Any]]] = []
        for item in raw_stmts:
            if not isinstance(item, dict):
                raise RuntimeError(f"Invalid statement entry from model: {item!r}")
            stmt_sql = str(item.get("sql") or "").strip()
            stmt_params = list(item.get("params") or [])
            if not stmt_sql:
                raise RuntimeError("Model produced an empty SQL statement")
            sql_pairs.append((stmt_sql, stmt_params))

        description = str(args.get("description") or "Gemini repair plan")
        result = execute_write(
            sql=sql_pairs,
            agent_name=f"Gemini ({reasoning_model})",
            description=description,
        )
        return {
            "flash_summary": flash_summary,
            "reasoning_plan": description,
            "skipped": False,
            "statements_executed_by_model": sql_pairs,
            "action": result,
        }

    # Explicit offline demo mode (`use_live_gemini=False`)
    if revert_notice:
        skip_reason = (
            "SKIP: human deliberately reviewed and reverted the repair; not re-applying reverted writes."
        )
        return {
            "flash_summary": "Diagnostic loop paused after human revert review.",
            "reasoning_plan": skip_reason,
            "skipped": True,
            "skip_reason": skip_reason,
            "action": None,
        }

    if _is_bench_corrupted(state):
        flash_summary = (
            "Deviations from baseline: (1) GPIO12 is OUTPUT_HIGH @ 3300mV instead of ADC_IN @ 1800mV; "
            "(2) GPIO4/GPIO5 pull_up=false instead of true; (3) calibration rows temp_ntc (+8mV) and vbat_adc (-12mV) missing."
        )
        reasoning_plan = (
            "1. Restore GPIO12 to ADC_IN @ 1800mV.\n"
            "2. Re-enable pull-ups on GPIO4/GPIO5 for I2C.\n"
            "3. Restore calibration rows temp_ntc (+8mV) and vbat_adc (-12mV) and log repair."
        )
        statements = [
            (
                "UPDATE pin_configs SET mode = %s, voltage_mv = %s WHERE board_id = %s AND gpio_pin = %s",
                ("ADC_IN", 1800, board_id, 12),
            ),
            (
                "UPDATE pin_configs SET pull_up = %s WHERE board_id = %s AND gpio_pin IN (4, 5)",
                (True, board_id),
            ),
            (
                "INSERT INTO calibration (board_id, sensor, offset_mv) VALUES (%s, %s, %s), (%s, %s, %s) "
                "ON CONFLICT (board_id, sensor) DO UPDATE SET offset_mv = EXCLUDED.offset_mv",
                (board_id, "vbat_adc", -12, board_id, "temp_ntc", 8),
            ),
            (
                "INSERT INTO diag_logs (board_id, message) VALUES (%s, %s)",
                (
                    board_id,
                    "Offline agent repaired bench: restored GPIO12 ADC_IN 1800mV, I2C pull-ups, and calibration.",
                ),
            ),
        ]
        result = execute_write(
            sql=statements,
            agent_name="Gemini (offline repair)",
            description="Repair corrupted bench: restore GPIO12 ADC_IN, I2C pull-ups, and calibration",
        )
        return {
            "flash_summary": flash_summary,
            "reasoning_plan": reasoning_plan,
            "skipped": False,
            "action": result,
        }

    # Healthy bench + offline mode (used by test_6 & test_core_vs_demo) simulates the bad write
    flash_summary = (
        "Flash loop observed transient vbat_adc=1800mV on GPIO12 and I2C pull-up draw on GPIO4/GPIO5."
    )
    reasoning_plan = (
        "1. Drive GPIO12 to OUTPUT_HIGH @ 3300mV to stabilize rail.\n"
        "2. Disable pull-ups on GPIO4/GPIO5 to reduce leakage.\n"
        "3. Clear stale calibration offsets for re-baselining."
    )
    statements = [
        (
            "UPDATE pin_configs SET mode = %s, voltage_mv = %s WHERE board_id = %s AND gpio_pin = %s",
            ("OUTPUT_HIGH", 3300, board_id, 12),
        ),
        (
            "UPDATE pin_configs SET pull_up = %s WHERE board_id = %s AND gpio_pin IN (4, 5)",
            (False, board_id),
        ),
        (
            "DELETE FROM calibration WHERE board_id = %s",
            (board_id,),
        ),
        (
            "INSERT INTO diag_logs (board_id, message) VALUES (%s, %s)",
            (
                board_id,
                "Agent applied brownout fix: GPIO12->OUTPUT_HIGH 3300mV, I2C pull-ups disabled, calibration cleared.",
            ),
        ),
    ]
    result = execute_write(
        sql=statements,
        agent_name="Gemini: Reconfigure power pins",
        description="Brownout fix: set GPIO12 OUTPUT_HIGH 3300mV, disable I2C pull-ups, wipe calibration",
    )
    return {
        "flash_summary": flash_summary,
        "reasoning_plan": reasoning_plan,
        "skipped": False,
        "action": result,
    }
