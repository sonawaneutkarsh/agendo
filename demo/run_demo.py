"""Runnable demo script for Agendo's ESP32 brownout-diagnosis scenario."""

from __future__ import annotations

import argparse
import json

from agendo.agent import run_diagnosis_and_repair
from agendo.restore import register_post_restore_hook, restore_action
from demo.hardware_hook import esp32_post_restore_hook


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Agendo ESP32 demo scenario")
    parser.add_argument(
        "--live-gemini",
        action="store_true",
        help="Call live Gemini API (requires GEMINI_API_KEY env var)",
    )
    parser.add_argument(
        "--auto-revert",
        action="store_true",
        help="Immediately revert the bad write, feed REVERT NOTICE back to agent, and print result",
    )
    args = parser.parse_args()

    register_post_restore_hook(esp32_post_restore_hook)

    print("1. Running Gemini ESP32 diagnosis & repair agent...")
    outcome = run_diagnosis_and_repair(use_live_gemini=args.live_gemini)
    print("Flash summary:", outcome["flash_summary"])
    print("Reasoning plan:\n" + outcome["reasoning_plan"])
    print("Action recorded:", json.dumps(outcome["action"], indent=2))

    if args.auto_revert and outcome.get("action"):
        action_id = outcome["action"]["action_id"]
        print(f"\n2. Reverting action {action_id}...")
        revert_res = restore_action(action_id)
        print("Revert result:", json.dumps(revert_res, indent=2, default=str))

        print("\n3. Feeding REVERT NOTICE back into agent...")
        followup = run_diagnosis_and_repair(
            use_live_gemini=args.live_gemini,
            revert_notice=revert_res["revert_notice"],
        )
        print("Follow-up skipped?", followup["skipped"])
        print("Follow-up reasoning plan:", followup["reasoning_plan"])


if __name__ == "__main__":
    main()
