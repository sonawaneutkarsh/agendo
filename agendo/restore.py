"""Deterministic, atomic, conflict-aware core restore engine for Agendo.

Core restore is completely application/hardware-agnostic. Domain-specific
post-restore side effects (such as pushing ESP32 config after DB restore)
are registered via `register_post_restore_hook()` or passed as `post_restore_hook`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agendo.db import get_connection

logger = logging.getLogger("agendo.restore")

PostRestoreHook = Callable[[psycopg.Connection, str, list[dict[str, Any]]], Any]
_POST_RESTORE_HOOKS: list[PostRestoreHook] = []


def register_post_restore_hook(hook: PostRestoreHook) -> None:
    """Register a callback executed inside the restore transaction after rows revert."""
    if hook not in _POST_RESTORE_HOOKS:
        _POST_RESTORE_HOOKS.append(hook)


def clear_post_restore_hooks() -> None:
    """Clear registered post-restore hooks (useful for tests)."""
    _POST_RESTORE_HOOKS.clear()


class ActionNotFoundError(LookupError):
    """Raised when an action_id does not exist."""


class ConflictError(RuntimeError):
    """Raised when a row changed after the agent's write and cannot be safely reverted."""

    def __init__(
        self,
        action_id: str,
        table_name: str,
        pk: dict[str, Any],
        old_row: dict[str, Any] | None,
        agent_new: dict[str, Any] | None,
        current_row: dict[str, Any] | None,
        reason: str = "Row modified after agent write",
    ) -> None:
        self.action_id = action_id
        self.table_name = table_name
        self.pk = pk
        self.old_row = old_row
        self.agent_new = agent_new
        self.current_row = current_row
        self.reason = reason
        super().__init__(
            f"Conflict restoring action {action_id} on {table_name} pk={pk}: {reason}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "conflict",
            "action_id": self.action_id,
            "table_name": self.table_name,
            "pk": self.pk,
            "reason": self.reason,
            "three_way_diff": {
                "old": self.old_row,
                "agent_new": self.agent_new,
                "current": self.current_row,
            },
        }


def _where_pk(pk: dict[str, Any]) -> tuple[sql.Composed, list[Any]]:
    clauses: list[sql.Composed] = []
    values: list[Any] = []
    for col, val in pk.items():
        clauses.append(sql.SQL("{} = %s").format(sql.Identifier(col)))
        values.append(val)
    return sql.SQL(" AND ").join(clauses), values


def _fetch_current_row(
    conn: psycopg.Connection, table_name: str, pk: dict[str, Any]
) -> dict[str, Any] | None:
    where_sql, where_vals = _where_pk(pk)
    query = sql.SQL("SELECT to_jsonb(t) AS row_json FROM {} t WHERE {}").format(
        sql.Identifier(table_name),
        where_sql,
    )
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(query, where_vals).fetchone()
    return row["row_json"] if row else None


def _get_writable_columns(conn: psycopg.Connection, table_name: str) -> list[str]:
    """Return live non-dropped, non-generated column names for schema-safe DELETE undo."""
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT a.attname
            FROM pg_attribute a
            JOIN pg_class cl ON cl.oid = a.attrelid
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace
            WHERE ns.nspname = ANY(current_schemas(false))
              AND cl.relname = %s
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND a.attgenerated = ''
            ORDER BY a.attnum
            """,
            (table_name,),
        ).fetchall()
    return [r["attname"] for r in rows]


def _build_revert_notice(
    action_id: str, description: str, restored_rows: list[dict[str, Any]]
) -> str:
    lines = [
        f"REVERT NOTICE: Action {action_id} ({description or 'no description'}) was reverted.",
        f"Restored {len(restored_rows)} row(s) to their exact prior state:",
    ]
    for item in restored_rows:
        lines.append(
            f"- {item['table_name']} pk={item['pk']} (undid {item['op']}); "
            f"restored={item['restored_to']}"
        )
    lines.append(
        "Do NOT re-apply the reverted changes without re-evaluating the current database state."
    )
    return "\n".join(lines)


def restore_action(
    action_id: str,
    conn: psycopg.Connection | None = None,
    post_restore_hook: PostRestoreHook | None = None,
    _inject_failure_after: int | None = None,
) -> dict[str, Any]:
    """Deterministically revert all row diffs for `action_id` in a single atomic transaction."""
    t0 = time.perf_counter()

    def _run(c: psycopg.Connection) -> dict[str, Any]:
        with c.cursor(row_factory=dict_row) as cur:
            action = cur.execute(
                """
                SELECT action_id::text AS action_id, agent_name, description, status
                FROM agendo_actions
                WHERE action_id = %s::uuid
                """,
                (action_id,),
            ).fetchone()
            if action is None:
                raise ActionNotFoundError(f"Action {action_id} not found")

            # Idempotency: second undo is a clean no-op
            if action["status"] == "reverted":
                return {
                    "status": "already_reverted",
                    "action_id": action_id,
                    "restored_rows": [],
                    "revert_notice": f"REVERT NOTICE: Action {action_id} was already reverted.",
                    "hook_results": [],
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                }

            # Reverse chronological seq order ensures FK-safe undo:
            # - DELETE undo restores parent rows before child rows
            # - INSERT undo deletes child rows before parent rows
            diffs = list(
                cur.execute(
                    """
                    SELECT id, seq, table_name, pk, op, old_row, new_row
                    FROM agendo_row_diffs
                    WHERE action_id = %s::uuid
                    ORDER BY seq DESC
                    """,
                    (action_id,),
                ).fetchall()
            )

        restored_rows: list[dict[str, Any]] = []
        hook_results: list[Any] = []

        try:
            with c.transaction():
                # Suppress trigger capture during restore
                c.execute("SELECT set_config('agendo.is_restoring', 'true', true)")

                for idx, diff in enumerate(diffs):
                    if _inject_failure_after is not None and idx >= _inject_failure_after:
                        raise RuntimeError("Injected mid-undo failure for atomicity test")

                    table_name: str = diff["table_name"]
                    pk: dict[str, Any] = diff["pk"]
                    op: str = diff["op"]
                    old_row: dict[str, Any] | None = diff["old_row"]
                    new_row: dict[str, Any] | None = diff["new_row"]

                    # For UPDATE, if a PK column was updated, look up row by its new PK
                    lookup_pk = (
                        {k: new_row[k] for k in pk if new_row and k in new_row}
                        if op == "UPDATE" and new_row is not None
                        else pk
                    )
                    current_row = _fetch_current_row(c, table_name, lookup_pk)

                    # Version 1: compare the whole row.
                    # Version 2 should compare only the columns the agent changed.
                    if op in ("INSERT", "UPDATE"):
                        if current_row != new_row:
                            raise ConflictError(
                                action_id=action_id,
                                table_name=table_name,
                                pk=pk,
                                old_row=old_row,
                                agent_new=new_row,
                                current_row=current_row,
                            )
                    elif op == "DELETE":
                        if current_row is not None:
                            raise ConflictError(
                                action_id=action_id,
                                table_name=table_name,
                                pk=pk,
                                old_row=old_row,
                                agent_new=None,
                                current_row=current_row,
                            )

                    where_sql, where_vals = _where_pk(lookup_pk)

                    try:
                        if op == "INSERT":
                            del_sql = sql.SQL("DELETE FROM {} WHERE {}").format(
                                sql.Identifier(table_name),
                                where_sql,
                            )
                            c.execute(del_sql, where_vals)

                        elif op == "DELETE":
                            assert old_row is not None
                            # Schema-evolution-safe DELETE undo: only insert columns that still
                            # exist in the live table and are writable (skip dropped/generated cols;
                            # omit newly added columns so Postgres uses their DEFAULT).
                            live_cols = _get_writable_columns(c, table_name)
                            insert_cols = [col for col in live_cols if col in old_row]
                            if not insert_cols:
                                raise RuntimeError(
                                    f"No matching columns found to restore DELETE on {table_name}"
                                )
                            col_idents = [sql.Identifier(col) for col in insert_cols]
                            placeholders = [sql.Placeholder() for _ in insert_cols]
                            vals = [
                                Jsonb(old_row[col])
                                if isinstance(old_row[col], (dict, list))
                                else old_row[col]
                                for col in insert_cols
                            ]
                            ins_sql = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                                sql.Identifier(table_name),
                                sql.SQL(", ").join(col_idents),
                                sql.SQL(", ").join(placeholders),
                            )
                            c.execute(ins_sql, vals)

                        elif op == "UPDATE":
                            assert old_row is not None and new_row is not None
                            live_cols = set(_get_writable_columns(c, table_name))
                            # Build SET clause dynamically per changed column from old_row
                            set_clauses: list[sql.Composed] = []
                            set_vals: list[Any] = []
                            for col, old_val in old_row.items():
                                if col not in live_cols:
                                    continue
                                if new_row.get(col) != old_val:
                                    set_clauses.append(
                                        sql.SQL("{} = %s").format(sql.Identifier(col))
                                    )
                                    set_vals.append(
                                        Jsonb(old_val)
                                        if isinstance(old_val, (dict, list))
                                        else old_val
                                    )
                            if set_clauses:
                                upd_sql = sql.SQL("UPDATE {} SET {} WHERE {}").format(
                                    sql.Identifier(table_name),
                                    sql.SQL(", ").join(set_clauses),
                                    where_sql,
                                )
                                c.execute(upd_sql, set_vals + where_vals)
                    except psycopg.errors.ForeignKeyViolation as exc:
                        raise ConflictError(
                            action_id=action_id,
                            table_name=table_name,
                            pk=pk,
                            old_row=old_row,
                            agent_new=new_row,
                            current_row=current_row,
                            reason=f"ForeignKeyViolation: {exc.diag.message_primary or str(exc)}",
                        ) from exc

                    restored_rows.append(
                        {
                            "table_name": table_name,
                            "pk": pk,
                            "op": op,
                            "restored_to": old_row,
                        }
                    )

                c.execute(
                    "UPDATE agendo_actions SET status = 'reverted' WHERE action_id = %s::uuid",
                    (action_id,),
                )

                hooks = list(_POST_RESTORE_HOOKS)
                if post_restore_hook is not None and post_restore_hook not in hooks:
                    hooks.append(post_restore_hook)
                for hook in hooks:
                    res = hook(c, action_id, restored_rows)
                    if res is not None:
                        hook_results.append(res)

        except ConflictError:
            # Persist conflict status in a fresh transaction after rollback
            with c.transaction():
                c.execute(
                    "UPDATE agendo_actions SET status = 'conflict' WHERE action_id = %s::uuid",
                    (action_id,),
                )
            raise

        notice = _build_revert_notice(action_id, action["description"], restored_rows)
        return {
            "status": "reverted",
            "action_id": action_id,
            "restored_rows": restored_rows,
            "revert_notice": notice,
            "hook_results": hook_results,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    if conn is not None:
        return _run(conn)
    with get_connection() as c:
        return _run(c)
