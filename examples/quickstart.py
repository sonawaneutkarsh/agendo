"""Generic Postgres 16 quickstart for Agendo.

Requires only a running Postgres 16 database (no ESP32 bench, no Gemini key,
and no demo seed data).

Start plain Postgres 16:
    docker run -d --name agendo-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16-alpine
    export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres

Run:
    python examples/quickstart.py
"""

from __future__ import annotations

import agendo


def fetch_orders(conn) -> list[dict]:
    return list(
        conn.execute(
            "SELECT order_id, customer, status, total_cents FROM orders ORDER BY order_id"
        ).fetchall()
    )


def print_orders(label: str, rows: list[dict]) -> None:
    print(f"\n{label}:")
    for row in rows:
        print(
            f"  order_id={row['order_id']} customer={row['customer']} "
            f"status={row['status']} total_cents={row['total_cents']}"
        )


def main() -> None:
    with agendo.get_connection() as conn:
        # 1. Install Agendo's schema, trigger function, and helper (idempotent).
        agendo.install_schema(conn=conn)

        # 2. Create a plain table with a primary key and seed initial rows
        #    with capture suppressed so setup writes do not pollute the action log.
        with conn.transaction():
            conn.execute("SELECT set_config('agendo.is_restoring', 'true', true)")
            conn.execute("DROP TABLE IF EXISTS orders")
            conn.execute(
                """
                CREATE TABLE orders (
                    order_id INT PRIMARY KEY,
                    customer TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_cents INT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO orders (order_id, customer, status, total_cents)
                VALUES
                    (1, 'alice', 'paid', 4900),
                    (2, 'bob', 'paid', 12500),
                    (3, 'carol', 'pending', 3200)
                """
            )

        # 3. Attach Agendo's row-level capture trigger.
        attached = agendo.attach_trigger("orders", conn=conn)
        print(f"Attached trigger to: {attached}")

        original_rows = fetch_orders(conn)
        original_hash = agendo.table_row_hash("orders", "order_id", conn)
        print_orders("Original state", original_rows)

        # 4. Run a bad agent-style write: a wrong UPDATE and a DELETE in one action.
        action = agendo.execute_write(
            sql=[
                (
                    "UPDATE orders SET status = %s, total_cents = %s WHERE order_id = %s",
                    ("cancelled", 0, 1),
                ),
                (
                    "DELETE FROM orders WHERE order_id = %s",
                    (3,),
                ),
            ],
            agent_name="billing-agent",
            description="Mistakenly cancel order 1 and delete order 3",
            conn=conn,
        )
        print(
            f"\nRecorded action {action['action_id']} "
            f"({action['diffs_captured']} row diffs captured)."
        )

        corrupted_rows = fetch_orders(conn)
        print_orders("Corrupted state", corrupted_rows)
        assert corrupted_rows != original_rows

        # 5. Revert the action atomically.
        revert = agendo.restore_action(action["action_id"], conn=conn)
        print(f"\nRevert status: {revert['status']} ({revert['elapsed_ms']} ms)")
        print(revert["revert_notice"])

        restored_rows = fetch_orders(conn)
        print_orders("Restored state", restored_rows)

        # 6. Verify exact row-for-row restoration.
        assert revert["status"] == "reverted"
        assert restored_rows == original_rows
        assert agendo.table_row_hash("orders", "order_id", conn) == original_hash
        print("\nVerified: restored rows match original state exactly.")


if __name__ == "__main__":
    main()
