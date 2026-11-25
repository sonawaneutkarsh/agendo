"""Demo-only FastAPI router exposing the ESP32 test-bench state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from agendo.db import get_connection

router = APIRouter()


@router.get("/bench")
def api_bench_state() -> dict[str, Any]:
    """Return current ESP32 test-bench state and persisted hardware push log."""
    with get_connection() as conn:
        devices = list(
            conn.execute("SELECT * FROM esp32_devices ORDER BY board_id").fetchall()
        )
        pins = list(
            conn.execute(
                "SELECT * FROM pin_configs ORDER BY board_id, gpio_pin"
            ).fetchall()
        )
        calibration = list(
            conn.execute(
                "SELECT * FROM calibration ORDER BY board_id, sensor"
            ).fetchall()
        )
        logs = list(
            conn.execute(
                "SELECT * FROM diag_logs ORDER BY id DESC LIMIT 10"
            ).fetchall()
        )
        pushes = list(
            conn.execute(
                """
                SELECT id, action_id::text AS action_id, board_id, transport, pins_pushed, pushed_at
                FROM agendo_hardware_pushes
                ORDER BY id DESC
                LIMIT 5
                """
            ).fetchall()
        )

    i2c_ok = (
        sum(
            1
            for p in pins
            if p["gpio_pin"] in (4, 5) and p["pull_up"] and p["mode"].startswith("I2C_")
        )
        == 2
    )
    adc_ok = any(
        p["gpio_pin"] == 12 and p["mode"] == "ADC_IN" and p["voltage_mv"] == 1800
        for p in pins
    )
    cal_ok = len(calibration) >= 2

    return {
        "devices": devices,
        "pins": pins,
        "calibration": calibration,
        "logs": logs,
        "health": {
            "i2c_bus": "online" if i2c_ok else "offline",
            "gpio12_adc": "ok" if adc_ok else "misconfigured",
            "calibration": "ok" if cal_ok else "missing",
            "overall": "healthy" if (i2c_ok and adc_ok and cal_ok) else "corrupted",
        },
        "hardware_push_log": pushes,
    }
