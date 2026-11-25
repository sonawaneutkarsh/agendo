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
            "Abort or skip making any database writes when the bench is already healthy "
            "or when a REVERT NOTICE indicates the proposed change was rejected."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Explanation of why no database writes should be performed.",
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

    return genai.Client(api_key=api_key)


def run_diagnosis_and_repair(
    board_id: str = "esp32-bench-01",
    use_live_gemini: bool = True,
    revert_notice: str | None = None,
    genai_client: Any = None,
) -> dict[str, Any]:
    """Run Flash diagnosis + Hydrogen reasoning with tool calling, executing writes via Agendo.

    - When `use_live_gemini=True`: fails fast if `GEMINI_API_KEY` is missing (unless a
      test `genai_client` is injected), requires valid tool calls from the model, and
      never falls back to hardcoded SQL or Flash on error.
    - When `use_live_gemini=False`: runs the deterministic offline demo scenario, and
      respects `revert_notice` by refusing to re-apply previously reverted writes.
    """
    state = _read_bench_snapshot()
    notice_block = f"\n\n{revert_notice}\n" if revert_notice else ""

    if use_live_gemini:
        from google.genai import types

        client = genai_client or _get_genai_client()

        diag_prompt = (
            "You are an embedded systems diagnostic loop (Gemini Flash). "
            f"Inspect this ESP32 bench state for board '{board_id}' and summarize any "
            "brownout, voltage, I2C pull-up, or calibration observations concisely.\n"
            f"Bench state:\n{json.dumps(state, default=str)}{notice_block}"
        )
        flash_resp = client.models.generate_content(
            model=FLASH_MODEL,
            contents=diag_prompt,
        )
        flash_summary = (flash_resp.text or "").strip()

        repair_tool = _build_repair_tools()
        system_instruction = (
            "You are a hardware-bench SQL repair agent operating behind Agendo. "
            "You must decide whether to mutate Postgres tables (`pin_configs`, `calibration`, `diag_logs`) "
            "by calling `apply_sql_repair`, OR refrain from writing by calling `skip_repair`. "
            "CRITICAL RULE: If a REVERT NOTICE is present, a previous repair action was reverted as bad. "
            "You MUST NOT re-apply the reverted mutations; call `skip_repair` or propose a safe non-destructive step."
        )
        plan_prompt = (
            f"Flash diagnostic summary:\n{flash_summary}\n\n"
            f"Current database state:\n{json.dumps(state, default=str)}"
            f"{notice_block}\n\n"
            "Call `apply_sql_repair` with valid PostgreSQL parameterized SQL (%s placeholders) if a safe repair "
            "is needed, or call `skip_repair` if no write should happen."
        )

        try:
            plan_resp = client.models.generate_content(
                model=REASONING_MODEL,
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
                REASONING_MODEL,
                exc,
            )
            raise

        function_calls = getattr(plan_resp, "function_calls", None) or []
        if not function_calls:
            raise RuntimeError(
                f"Model {REASONING_MODEL} produced no valid tool call (text={plan_resp.text!r})"
            )

        call = function_calls[0]
        call_name = getattr(call, "name", "")
        args = dict(getattr(call, "args", {}) or {})

        if call_name == "skip_repair":
            reason = str(args.get("reason") or "Model chose to skip repair.")
            return {
                "flash_summary": flash_summary,
                "reasoning_plan": f"SKIPPED: {reason}",
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
            agent_name=f"Gemini ({REASONING_MODEL})",
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
    flash_summary = (
        "Flash loop observed transient vbat_adc=1800mV on GPIO12 and I2C pull-up draw on GPIO4/GPIO5."
    )
    if revert_notice:
        skip_reason = (
            "Offline agent received REVERT NOTICE and refused to re-apply the reverted GPIO12/I2C/calibration writes."
        )
        return {
            "flash_summary": flash_summary,
            "reasoning_plan": f"SKIPPED: {skip_reason}",
            "skipped": True,
            "skip_reason": skip_reason,
            "action": None,
        }

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
