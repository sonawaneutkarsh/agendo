"""ESP32 demo post-restore hook persisting mock serial pushes to Postgres."""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger("agendo.demo.hardware")

ESP32_TABLES = frozenset({"pin_configs", "esp32_devices", "calibration"})


def esp32_post_restore_hook(
    conn: psycopg.Connection,
    action_id: str,
    restored_rows: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Persist mock serial desired-state re-pushes into agendo_hardware_pushes."""
    board_ids: set[str] = set()
    for item in restored_rows:
        if item["table_name"] in ESP32_TABLES:
            pk = item.get("pk") or {}
            row = item.get("restored_to") or {}
            board_id = pk.get("board_id") or row.get("board_id")
            if board_id:
                board_ids.add(board_id)

    if not board_ids:
        return None

    pushes: list[dict[str, Any]] = []
    for board_id in sorted(board_ids):
        pins = list(
            conn.execute(
                """
                SELECT gpio_pin, mode, voltage_mv, pull_up
                FROM pin_configs
                WHERE board_id = %s
                ORDER BY gpio_pin ASC
                """,
                (board_id,),
            ).fetchall()
        )
        transport = "mock-serial://esp32-bench"
        inserted = conn.execute(
            """
            INSERT INTO agendo_hardware_pushes (action_id, board_id, transport, pins_pushed)
            VALUES (%s::uuid, %s, %s, %s::jsonb)
            RETURNING id, action_id::text AS action_id, board_id, transport, pins_pushed, pushed_at
            """,
            (action_id, board_id, transport, Jsonb(pins)),
        ).fetchone()
        logger.info("MOCK HARDWARE PUSH %s: %s", board_id, pins)
        if inserted:
            pushes.append(inserted)
    return pushes
