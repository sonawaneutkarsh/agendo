"""Capture inspection, trigger attachment, and deterministic table-hash helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
from psycopg import sql

from agendo.db import get_connection


def attach_trigger(table_name: str, conn: psycopg.Connection | None = None) -> str:
    """Attach agendo_capture() to `table_name` using its PRIMARY KEY columns."""

    def _run(c: psycopg.Connection) -> str:
        row = c.execute(
            "SELECT attach_agendo_trigger(%s::regclass) AS attached",
            (table_name,),
        ).fetchone()
        return row["attached"] if row else table_name

    if conn is not None:
        return _run(conn)
    with get_connection() as c:
        return _run(c)


def count_actions(conn: psycopg.Connection | None = None) -> int:
    """Return total number of tracked actions plus 1 if untracked writes exist."""

    def _run(c: psycopg.Connection) -> int:
        tracked = c.execute("SELECT COUNT(*)::int AS cnt FROM agendo_actions").fetchone()
        untracked = c.execute(
            "SELECT EXISTS(SELECT 1 FROM agendo_row_diffs WHERE action_id IS NULL) AS has_untracked"
        ).fetchone()
        total = tracked["cnt"] if tracked else 0
        if untracked and untracked["has_untracked"]:
            total += 1
        return total

    if conn is not None:
        return _run(conn)
    with get_connection() as c:
        return _run(c)


def list_actions(
    limit: int = 50,
    offset: int = 0,
    conn: psycopg.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return recorded actions newest-first with pagination, plus synthetic untracked entry."""
    tracked_query = """
        SELECT
            a.action_id::text AS action_id,
            a.agent_name,
            a.description,
            a.status,
            a.created_at,
            COUNT(d.id)::int AS row_count
        FROM agendo_actions a
        LEFT JOIN agendo_row_diffs d ON d.action_id = a.action_id
        GROUP BY a.action_id
        ORDER BY a.created_at DESC
    """
    untracked_query = """
        SELECT
            COUNT(*)::int AS row_count,
            MAX(created_at) AS latest_at
        FROM agendo_row_diffs
        WHERE action_id IS NULL
    """

    def _run(c: psycopg.Connection) -> list[dict[str, Any]]:
        untracked = c.execute(untracked_query).fetchone()
        has_untracked = bool(untracked and untracked["row_count"] > 0)

        rows: list[dict[str, Any]] = []
        if has_untracked:
            untracked_entry = {
                "action_id": "untracked",
                "agent_name": "UNKNOWN (BYPASSED SDK)",
                "description": "UNTRACKED WRITE DETECTED — raw SQL mutated watched tables without agendo.action_id",
                "status": "untracked",
                "created_at": untracked["latest_at"],
                "row_count": untracked["row_count"],
            }
            if offset == 0:
                rows.append(untracked_entry)
                remaining_limit = max(limit - 1, 0)
                db_offset = 0
            else:
                remaining_limit = limit
                db_offset = offset - 1
        else:
            remaining_limit = limit
            db_offset = offset

        if remaining_limit > 0:
            tracked_rows = list(
                c.execute(
                    tracked_query + " LIMIT %s OFFSET %s",
                    (remaining_limit, db_offset),
                ).fetchall()
            )
            rows.extend(tracked_rows)

        return rows

    if conn is not None:
        return _run(conn)
    with get_connection() as c:
        return _run(c)


def get_action_detail(
    action_id: str, conn: psycopg.Connection | None = None
) -> dict[str, Any] | None:
    """Return action metadata and ordered row diffs, or None if not found."""

    def _run(c: psycopg.Connection) -> dict[str, Any] | None:
        if action_id == "untracked":
            diffs = list(
                c.execute(
                    """
                    SELECT id, NULL::text AS action_id, seq, table_name, pk, op, old_row, new_row, created_at
                    FROM agendo_row_diffs
                    WHERE action_id IS NULL
                    ORDER BY seq ASC
                    """
                ).fetchall()
            )
            if not diffs:
                return None
            return {
                "action_id": "untracked",
                "agent_name": "UNKNOWN (BYPASSED SDK)",
                "description": "UNTRACKED WRITE DETECTED",
                "status": "untracked",
                "created_at": diffs[-1]["created_at"],
                "diffs": diffs,
            }

        action = c.execute(
            """
            SELECT action_id::text AS action_id, agent_name, description, status, created_at
            FROM agendo_actions
            WHERE action_id = %s::uuid
            """,
            (action_id,),
        ).fetchone()
        if action is None:
            return None

        diffs = list(
            c.execute(
                """
                SELECT id, action_id::text AS action_id, seq, table_name, pk, op, old_row, new_row, created_at
                FROM agendo_row_diffs
                WHERE action_id = %s::uuid
                ORDER BY seq ASC
                """,
                (action_id,),
            ).fetchall()
        )
        return {**action, "diffs": diffs}

    if conn is not None:
        return _run(conn)
    with get_connection() as c:
        return _run(c)


def table_row_hash(
    table_name: str, order_by: str, conn: psycopg.Connection
) -> str:
    """Compute a deterministic MD5 hash over ordered JSONB rows for exact round-trip tests."""
    query = sql.SQL("SELECT to_jsonb(t) AS row_json FROM {} t ORDER BY {}").format(
        sql.Identifier(table_name),
        sql.Identifier(order_by),
    )
    rows = conn.execute(query).fetchall()
    canonical = json.dumps(
        [r["row_json"] for r in rows], sort_keys=True, separators=(",", ":")
    )
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()
