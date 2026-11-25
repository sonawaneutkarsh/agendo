"""Self-contained runnable demo for Agendo's ESP32 brownout-diagnosis scenario."""

from __future__ import annotations

import argparse
import json
from typing import Any

from agendo.agent import run_diagnosis_and_repair
from agendo.db import get_connection
from agendo.proxy import execute_write
from agendo.restore import register_post_restore_hook, restore_action
from demo.hardware_hook import esp32_post_restore_hook


def corrupt_board(board_id: str = "esp32-bench-01") -> dict[str, Any]:
    """Ensure baseline rows exist, then corrupt the ESP32 bench via Agendo:
    - GPIO12 wrongly set to OUTPUT_HIGH at 3300mV
    - I2C pull-ups (GPIO4/GPIO5) disabled
    - 2 calibration rows wiped
    """
    with get_connection() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('agendo.is_restoring', 'true', true)")
            conn.execute(
                """
                INSERT INTO pin_configs (board_id, gpio_pin, mode, voltage_mv, pull_up)
                VALUES
                    (%s, 4,  'I2C_SDA', 3300, true),
                    (%s, 5,  'I2C_SCL', 3300, true),
                    (%s, 12, 'ADC_IN',  1800, false)
                ON CONFLICT (board_id, gpio_pin) DO UPDATE
                    SET mode = EXCLUDED.mode,
                        voltage_mv = EXCLUDED.voltage_mv,
                        pull_up = EXCLUDED.pull_up
                """,
                (board_id, board_id, board_id),
            )
            conn.execute(
                """
                INSERT INTO calibration (board_id, sensor, offset_mv)
                VALUES
                    (%s, 'vbat_adc', -12),
                    (%s, 'temp_ntc', 8)
                ON CONFLICT (board_id, sensor) DO UPDATE
                    SET offset_mv = EXCLUDED.offset_mv
                """,
                (board_id, board_id),
            )

    return execute_write(
        sql=[
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
        ],
        agent_name="demo-fault-injector",
        description="Corrupt board: GPIO12 OUTPUT_HIGH 3300mV, I2C pull-ups disabled, 2 calibration rows wiped",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run self-contained Agendo ESP32 demo scenario")
    parser.add_argument(
        "--live-gemini",
        action="store_true",
        help="Call live Gemini API (requires GEMINI_API_KEY env var)",
    )
    parser.add_argument(
        "--auto-revert",
        action="store_true",
        default=True,
        help="Revert the agent's action and feed REVERT NOTICE back into the agent (default: enabled)",
    )
    parser.add_argument(
        "--no-revert",
        action="store_true",
        help="Stop after the agent writes without auto-reverting",
    )
    args = parser.parse_args()
    do_revert = args.auto_revert and not args.no_revert

    register_post_restore_hook(esp32_post_restore_hook)

    print("0. Corrupting ESP32 bench (GPIO12=OUTPUT_HIGH 3300mV, I2C pull-ups=false, calibration wiped)...")
    corrupt_res = corrupt_board()
    print("Corruption action recorded:", json.dumps(corrupt_res, indent=2))

    print("\n1. Running Gemini ESP32 diagnosis & repair agent...")
    outcome = run_diagnosis_and_repair(use_live_gemini=args.live_gemini)
    print("Flash summary:", outcome["flash_summary"])
    print("Reasoning plan:\n" + outcome["reasoning_plan"])
    print("Agent skipped?", outcome.get("skipped"))
    print("Agent action recorded:", json.dumps(outcome["action"], indent=2))

    if not outcome.get("action"):
        raise RuntimeError("Agent failed to produce a repair action on the corrupted board.")

    if do_revert:
        action_id = outcome["action"]["action_id"]
        print(f"\n2. Reverting agent action {action_id}...")
        revert_res = restore_action(action_id)
        print("Revert result:", json.dumps(revert_res, indent=2, default=str))

        print("\n3. Feeding REVERT NOTICE back into agent...")
        followup = run_diagnosis_and_repair(
            use_live_gemini=args.live_gemini,
            revert_notice=revert_res["revert_notice"],
        )
        print("Follow-up skipped?", followup["skipped"])
        print("Follow-up reasoning plan:", followup["reasoning_plan"])
        if not followup["skipped"] or followup["action"] is not None:
            raise RuntimeError("Agent failed to skip re-applying reverted action after REVERT NOTICE")


if __name__ == "__main__":
    main()
