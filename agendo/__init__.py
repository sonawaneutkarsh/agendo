"""Agendo: deterministic row-level rollback engine for AI agents on Postgres."""

from agendo.capture import attach_trigger, get_action_detail, list_actions, table_row_hash
from agendo.proxy import ActionIdCollisionError, PermissionDeniedError, execute_write
from agendo.restore import (
    ActionNotFoundError,
    ConflictError,
    clear_post_restore_hooks,
    register_post_restore_hook,
    restore_action,
)

__all__ = [
    "ActionIdCollisionError",
    "ActionNotFoundError",
    "ConflictError",
    "PermissionDeniedError",
    "attach_trigger",
    "clear_post_restore_hooks",
    "execute_write",
    "get_action_detail",
    "list_actions",
    "register_post_restore_hook",
    "restore_action",
    "table_row_hash",
]
