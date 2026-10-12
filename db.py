import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from werkzeug.security import generate_password_hash

from laundry_logic import STATUS_TRANSITIONS

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("FRESHNEST_DB_PATH", BASE_DIR / "freshnest.db")).resolve()

ORDER_FIELDS = {
    "user_id": "INTEGER REFERENCES users(id)",
    "address": "TEXT NOT NULL DEFAULT ''",
    "contact_number": "TEXT NOT NULL DEFAULT ''",
    "pickup_time": "TEXT NOT NULL DEFAULT '08:00'",
    "payment_status": "TEXT NOT NULL DEFAULT 'Unpaid'",
}


def get_db_connection():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db():
    with closing(get_db_connection()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('customer', 'admin')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                customer_name TEXT NOT NULL,
                service_type TEXT NOT NULL,
                weight_kg REAL NOT NULL,
                payment_method TEXT NOT NULL,
                total_price REAL NOT NULL,
                pickup_date TEXT NOT NULL,
                pickup_time TEXT NOT NULL DEFAULT '08:00',
                address TEXT NOT NULL DEFAULT '',
                contact_number TEXT NOT NULL DEFAULT '',
                payment_status TEXT NOT NULL DEFAULT 'Unpaid',
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TEXT NOT NULL
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(orders)").fetchall()
        }
        for column_name, definition in ORDER_FIELDS.items():
            if column_name not in existing_columns:
                connection.execute(
                    f"ALTER TABLE orders ADD COLUMN {column_name} {definition}"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, id DESC)"
        )
        connection.commit()


def restore_orders_from_backup(orders):
    """Restore legacy JSON records when the orders table is empty."""
    if not isinstance(orders, list) or not orders:
        return

    required_fields = (
        "customer_name",
        "service_type",
        "weight_kg",
        "payment_method",
        "total_price",
        "pickup_date",
        "created_at",
    )
    with closing(get_db_connection()) as connection:
        cursor = connection.cursor()
        if cursor.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
            return
        for order in orders:
            if not isinstance(order, dict) or any(
                field not in order for field in required_fields
            ):
                print("Warning: Skipping incomplete order in JSON backup.")
                continue
            try:
                weight = float(order["weight_kg"])
                total = float(order["total_price"])
                if not math.isfinite(weight) or weight <= 0 or not math.isfinite(total):
                    raise ValueError("Order contains an invalid weight or total.")
                cursor.execute(
                    """
                    INSERT INTO orders (
                        id, customer_name, service_type, weight_kg, payment_method,
                        total_price, pickup_date, pickup_time, address, contact_number,
                        payment_status, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order.get("id"),
                        order["customer_name"],
                        order["service_type"],
                        weight,
                        order["payment_method"],
                        total,
                        order["pickup_date"],
                        order.get("pickup_time", "08:00"),
                        order.get("address", ""),
                        order.get("contact_number", ""),
                        order.get("payment_status", "Unpaid"),
                        order.get("status", "Pending"),
                        order["created_at"],
                    ),
                )
            except (sqlite3.Error, TypeError, ValueError) as error:
                print(f"Warning: Could not restore an order from JSON backup ({error}).")
        connection.commit()


def get_user_by_email(email):
    with closing(get_db_connection()) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()


def get_user_by_id(user_id):
    with closing(get_db_connection()) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def create_user(full_name, email, password_hash, role="customer"):
    with closing(get_db_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            INSERT INTO users (full_name, email, password_hash, role)
            VALUES (?, ?, ?, ?)
            """,
            (full_name, email, password_hash, role),
        )
        connection.commit()
        return cursor.lastrowid


def provision_admin(full_name, email, password):
    """Create or refresh the environment-configured administrator account."""
    password_hash = generate_password_hash(password)
    with closing(get_db_connection()) as connection:
        connection.execute(
            """
            INSERT INTO users (full_name, email, password_hash, role)
            VALUES (?, ?, ?, 'admin')
            ON CONFLICT(email) DO UPDATE SET
                full_name = excluded.full_name,
                password_hash = excluded.password_hash,
                role = 'admin'
            """,
            (full_name, email, password_hash),
        )
        connection.commit()


def create_order(order):
    with closing(get_db_connection()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO orders (
                user_id, customer_name, service_type, weight_kg, payment_method,
                total_price, pickup_date, pickup_time, address, contact_number,
                payment_status, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Unpaid', 'Pending', ?)
            """,
            (
                order["user_id"],
                order["customer_name"],
                order["service_type"],
                order["weight_kg"],
                order["payment_method"],
                order["total_price"],
                order["pickup_date"],
                order["pickup_time"],
                order["address"],
                order["contact_number"],
                order["created_at"],
            ),
        )
        connection.commit()
        return cursor.lastrowid


def get_orders_for_user(user_id):
    with closing(get_db_connection()) as connection:
        return connection.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()


def get_order_for_user(order_id, user_id):
    with closing(get_db_connection()) as connection:
        return connection.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?",
            (order_id, user_id),
        ).fetchone()


def get_admin_orders(status=None, search=None, page=1, per_page=10):
    filters = []
    parameters = []
    if status and status != "All":
        filters.append("status = ?")
        parameters.append(status)
    if search:
        filters.append("(customer_name LIKE ? OR CAST(id AS TEXT) = ?)")
        parameters.extend((f"%{search}%", search.lstrip("#")))
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    with closing(get_db_connection()) as connection:
        stats = connection.execute(
            """
            SELECT COUNT(*) AS total_orders,
                   SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending_count,
                   COALESCE(SUM(CASE WHEN status != 'Cancelled' AND payment_status = 'Paid' THEN total_price ELSE 0 END), 0) AS revenue
            FROM orders
            """
        ).fetchone()
        total_matching = connection.execute(
            f"SELECT COUNT(*) FROM orders {where_clause}", parameters
        ).fetchone()[0]
        offset = (page - 1) * per_page
        orders = connection.execute(
            f"""
            SELECT * FROM orders {where_clause}
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (*parameters, per_page, offset),
        ).fetchall()
    return orders, stats, total_matching


def update_order_status(order_id, new_status):
    with closing(get_db_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        )
        order = cursor.fetchone()
        if order is None:
            return "not_found"
        if new_status == order["status"]:
            return "unchanged"
        if new_status not in STATUS_TRANSITIONS.get(order["status"], ()):
            return "invalid_transition"
        connection.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id)
        )
        connection.commit()
        return "updated"


def mark_order_paid(order_id):
    with closing(get_db_connection()) as connection:
        cursor = connection.execute(
            """
            UPDATE orders SET payment_status = 'Paid'
            WHERE id = ? AND payment_status = 'Unpaid' AND status != 'Cancelled'
            """,
            (order_id,),
        )
        connection.commit()
        return cursor.rowcount > 0


def cancel_pending_order(order_id, user_id):
    with closing(get_db_connection()) as connection:
        cursor = connection.execute(
            """
            UPDATE orders SET status = 'Cancelled'
            WHERE id = ? AND user_id = ? AND status = 'Pending'
            """,
            (order_id, user_id),
        )
        connection.commit()
        return cursor.rowcount > 0
