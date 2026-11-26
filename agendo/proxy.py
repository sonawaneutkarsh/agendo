"""Agendo SDK / SQL write proxy with statement-level sqlparse permission checks."""

from __future__ import annotations

import uuid
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row
import sqlparse
from sqlparse import tokens as T
from sqlparse.sql import Identifier, TokenList

from agendo.db import get_connection

# Hardcoded v1 permission deny-list enforced via sqlparse token inspection (not regex).
# DDL and procedural blocks are denied because row-level triggers cannot revert them.
FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "DROP",
        "TRUNCATE",
        "ALTER",
        "CREATE",
        "REPLACE",
        "GRANT",
        "REVOKE",
        "CALL",
        "EXECUTE",
        "VACUUM",
        "CLUSTER",
        "REINDEX",
        "COPY",
    }
)

FORBIDDEN_STATEMENT_TYPES: frozenset[str] = frozenset(
    {"DROP", "ALTER", "CREATE", "GRANT", "REVOKE"}
)

DENIED_TABLES: frozenset[str] = frozenset(
    {
        "agendo_actions",
        "agendo_row_diffs",
    }
)

DENIED_SCHEMAS: frozenset[str] = frozenset(
    {
        "pg_catalog",
        "information_schema",
    }
)


class PermissionDeniedError(PermissionError):
    """Raised when an agent write violates the pre-write statement/table deny-list."""


class ActionIdCollisionError(ValueError):
    """Raised when an explicitly supplied action_id already exists in agendo_actions."""


def _strip_ident(name: str) -> str:
    return name.strip().strip('"').lower()


def _is_non_identifier_literal_or_comment(token: Any) -> bool:
    """Skip single-quoted string literals, numbers, and comments — NOT double-quoted identifiers."""
    if token.ttype in T.Comment:
        return True
    if token.ttype == T.Literal.String.Symbol:
        return False
    return token.ttype in T.Literal


def _iter_identifiers(token_list: TokenList):
    for token in token_list.tokens:
        if _is_non_identifier_literal_or_comment(token):
            continue
        if isinstance(token, Identifier):
            yield token
        elif isinstance(token, TokenList):
            yield from _iter_identifiers(token)


def check_permissions(sql_text: str, extra_denied_tables: Sequence[str] = ()) -> None:
    """Reject DDL/procedural blocks and writes targeting denied tables or system schemas."""
    parsed_stmts = [s for s in sqlparse.parse(sql_text) if str(s).strip()]
    if not parsed_stmts:
        raise PermissionDeniedError("Permission denied: empty SQL statement.")

    denied_tables = DENIED_TABLES.union(t.lower() for t in extra_denied_tables)

    for stmt in parsed_stmts:
        stmt_type = (stmt.get_type() or "UNKNOWN").upper()
        if any(stmt_type.startswith(prefix) for prefix in FORBIDDEN_STATEMENT_TYPES):
            raise PermissionDeniedError(
                f"Permission denied: statement type '{stmt_type}' is blocked "
                "because row-level undo cannot revert schema or privilege changes."
            )

        # Block top-level anonymous DO $$ ... $$ procedural blocks while still
        # allowing valid PostgreSQL upserts (`INSERT ... ON CONFLICT DO UPDATE/NOTHING`).
        first_tok = stmt.token_first(skip_ws=True, skip_cm=True)
        if first_tok is not None and first_tok.value.upper() == "DO":
            raise PermissionDeniedError(
                "Permission denied: SQL keyword 'DO' is blocked "
                "because row-level undo cannot revert DDL or procedural blocks."
            )

        # Walk flattened tokens, skipping string literals and comments so values like
        # 'ALTER ego' or '-- DROP TABLE' never trigger false positives, while still
        # inspecting double-quoted SQL identifiers (T.Literal.String.Symbol).
        for tok in stmt.flatten():
            if _is_non_identifier_literal_or_comment(tok) or tok.is_whitespace:
                continue
            upper_val = tok.value.upper()
            if tok.ttype in T.Keyword:
                for kw_part in upper_val.split():
                    if kw_part in FORBIDDEN_KEYWORDS:
                        raise PermissionDeniedError(
                            f"Permission denied: SQL keyword '{upper_val}' is blocked "
                            "because row-level undo cannot revert DDL or procedural blocks."
                        )
            if tok.ttype in (T.Name, T.Literal.String.Symbol):
                seg = _strip_ident(tok.value)
                if seg in DENIED_SCHEMAS:
                    raise PermissionDeniedError(
                        f"Permission denied: schema '{seg}' is on the Agendo deny-list."
                    )
                if seg in denied_tables:
                    raise PermissionDeniedError(
                        f"Permission denied: table '{seg}' is on the Agendo deny-list."
                    )

        # Also inspect grouped identifiers (handles schema-qualified names like public.agendo_actions)
        for ident in _iter_identifiers(stmt):
            parts = [
                _strip_ident(sub.value)
                for sub in ident.tokens
                if sub.ttype not in T.Punctuation
                and not sub.is_whitespace
                and not _is_non_identifier_literal_or_comment(sub)
            ]
            expanded: list[str] = []
            for p in parts:
                expanded.extend(_strip_ident(seg) for seg in p.split(".") if seg)

            for seg in expanded:
                if seg in DENIED_SCHEMAS:
                    raise PermissionDeniedError(
                        f"Permission denied: schema '{seg}' is on the Agendo deny-list."
                    )
                if seg in denied_tables:
                    raise PermissionDeniedError(
                        f"Permission denied: table '{seg}' is on the Agendo deny-list."
                    )


def execute_write(
    sql: str | Sequence[tuple[str, Sequence[Any] | dict[str, Any] | None]],
    params: Sequence[Any] | dict[str, Any] | None = None,
    action_id: str | None = None,
    agent_name: str = "agent",
    description: str = "",
    conn: psycopg.Connection | None = None,
    allow_append: bool = False,
) -> dict[str, Any]:
    """Execute one or more SQL writes inside a single tracked Agendo transaction.

    Pass either a single `sql` string (+ optional `params`), or a sequence of
    `(sql, params)` pairs so multiple statements in one agent turn share one `action_id`.
    """
    statements: list[tuple[str, Any]]
    if isinstance(sql, str):
        statements = [(sql, params)]
    else:
        statements = [(stmt, p) for stmt, p in sql]

    if not statements:
        raise ValueError("execute_write requires at least one SQL statement")

    for stmt, _ in statements:
        check_permissions(stmt)

    resolved_action_id = str(uuid.UUID(action_id)) if action_id else str(uuid.uuid4())

    def _run(c: psycopg.Connection) -> dict[str, Any]:
        with c.transaction():
            with c.cursor(row_factory=dict_row) as cur:
                existing = cur.execute(
                    """
                    SELECT agent_name, status
                    FROM agendo_actions
                    WHERE action_id = %s::uuid
                    FOR UPDATE
                    """,
                    (resolved_action_id,),
                ).fetchone()

                if existing is not None:
                    if not allow_append:
                        raise ActionIdCollisionError(
                            f"action_id {resolved_action_id} already exists (status={existing['status']}). "
                            "Use a fresh action_id or pass allow_append=True."
                        )
                    if existing["status"] != "applied":
                        raise ActionIdCollisionError(
                            f"Cannot append to action_id {resolved_action_id} with status '{existing['status']}'."
                        )
                    if existing["agent_name"] != agent_name:
                        raise ActionIdCollisionError(
                            f"action_id {resolved_action_id} belongs to agent '{existing['agent_name']}', "
                            f"not '{agent_name}'."
                        )
                else:
                    cur.execute(
                        """
                        INSERT INTO agendo_actions (action_id, agent_name, description, status)
                        VALUES (%s::uuid, %s, %s, 'applied')
                        """,
                        (resolved_action_id, agent_name, description),
                    )

                # SET LOCAL scopes agendo.action_id strictly to this transaction
                cur.execute(
                    "SELECT set_config('agendo.action_id', %s, true)",
                    (resolved_action_id,),
                )
                rowcounts: list[int] = []
                for stmt, p in statements:
                    cur.execute(stmt, p)
                    rowcounts.append(cur.rowcount)

                diff_count_row = cur.execute(
                    "SELECT COUNT(*)::int AS cnt FROM agendo_row_diffs WHERE action_id = %s::uuid",
                    (resolved_action_id,),
                ).fetchone()
                diff_count = diff_count_row["cnt"] if diff_count_row else 0

        return {
            "action_id": resolved_action_id,
            "agent_name": agent_name,
            "description": description,
            "status": "applied",
            "statements_executed": len(statements),
            "rows_affected": sum(max(rc, 0) for rc in rowcounts),
            "diffs_captured": diff_count,
        }

    if conn is not None:
        return _run(conn)
    with get_connection() as c:
        return _run(c)
