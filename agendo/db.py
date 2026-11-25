"""Database connection helpers for Agendo."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row


def _load_dotenv_if_present() -> None:
    """Populate missing env vars from a local .env file without overwriting explicit env."""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ and val:
                os.environ[key] = val
        break


def get_database_url() -> str:
    """Resolve DATABASE_URL from environment (or local .env), failing fast if missing."""
    _load_dotenv_if_present()
    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError(
            "DATABASE_URL (or POSTGRES_PASSWORD) is not set. "
            "Copy .env.example to .env or export DATABASE_URL."
        )
    user = os.environ.get("POSTGRES_USER", "agendo")
    db = os.environ.get("POSTGRES_DB", "agendo")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


@contextmanager
def get_connection(dsn: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a psycopg v3 connection with dict_row factory and autocommit=True.

    autocommit=True ensures standalone SELECTs do not open an implicit outer
    transaction, so `with conn.transaction():` always creates a real BEGIN/COMMIT
    block and `SET LOCAL` / `set_config(..., true)` resets cleanly at block exit.
    """
    conn = psycopg.connect(
        dsn or get_database_url(),
        row_factory=dict_row,
        autocommit=True,
    )
    try:
        yield conn
    finally:
        conn.close()
