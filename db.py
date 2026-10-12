import datetime
import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from werkzeug.security import generate_password_hash

from laundry_logic import MANILA_TZ, STATUS_TRANSITIONS

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("FRESHNEST_DB_PATH", BASE_DIR / "freshnest.db")).resolve()

ORDER_FIELDS = {
    "user_id": "INTEGER REFERENCES users(id)",
    "address": "TEXT NOT NULL DEFAULT ''",
    "contact_number": "TEXT NOT NULL DEFAULT ''",
    "pickup_time": "TEXT NOT NULL DEFAULT '08:00'",
    "payment_status": "TEXT NOT NULL DEFAULT 'Unpaid'",
    "actual_weight_kg": "REAL",
    "payment_reference": "TEXT NOT NULL DEFAULT ''",
    "paid_at": "TEXT",
}


def get_db_connection():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db():
    with closing(get_db_connection()) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('customer', 'admin')),
                saved_address TEXT NOT NULL DEFAULT '',
                saved_contact_number TEXT NOT NULL DEFAULT '',
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
        existing_user_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        for column_name in ("saved_address", "saved_contact_number"):
            if column_name not in existing_user_columns:
                connection.execute(
                    f"ALTER TABLE users ADD COLUMN {column_name} TEXT NOT NULL DEFAULT ''"
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO order_events (order_id, status, changed_at, changed_by)
            SELECT orders.id, orders.status, orders.created_at, orders.customer_name
            FROM orders
            WHERE NOT EXISTS (
                SELECT 1 FROM order_events WHERE order_events.order_id = orders.id
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                email TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                PRIMARY KEY (email, ip_address)
            )
            """
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
                user_id = order.get("user_id")
                if user_id is not None and cursor.execute(
                    "SELECT 1 FROM users WHERE id = ?", (user_id,)
                ).fetchone() is None:
                    user_id = None
                cursor.execute(
                    """
                    INSERT INTO orders (
                        id, user_id, customer_name, service_type, weight_kg,
                        actual_weight_kg, payment_method, payment_reference, paid_at,
                        total_price, pickup_date, pickup_time, address, contact_number,
                        payment_status, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order.get("id"),
                        user_id,
                        order["customer_name"],
                        order["service_type"],
                        weight,
                        order.get("actual_weight_kg"),
                        order["payment_method"],
                        order.get("payment_reference", ""),
                        order.get("paid_at"),
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
                order_id = order.get("id") or cursor.lastrowid
                cursor.execute(
                    """
                    INSERT INTO order_events (order_id, status, changed_at, changed_by)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        order.get("status", "Pending"),
                        order["created_at"],
                        "JSON backup restore",
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


def get_login_lock_seconds(email, ip_address, now=None):
    current = now or datetime.datetime.now(datetime.timezone.utc)
    with closing(get_db_connection()) as connection:
        row = connection.execute(
            """
            SELECT locked_until FROM login_attempts
            WHERE email = ? AND ip_address = ?
            """,
            (email, ip_address),
        ).fetchone()
    if row is None or row["locked_until"] is None:
        return 0
    locked_until = datetime.datetime.fromisoformat(row["locked_until"])
    return max(0, math.ceil((locked_until - current).total_seconds()))


def record_login_failure(email, ip_address, now=None):
    current = now or datetime.datetime.now(datetime.timezone.utc)
    with closing(get_db_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT failures, locked_until FROM login_attempts
            WHERE email = ? AND ip_address = ?
            """,
            (email, ip_address),
        ).fetchone()
        if row is not None and row["locked_until"]:
            locked_until = datetime.datetime.fromisoformat(row["locked_until"])
            if locked_until > current:
                connection.commit()
                return max(1, math.ceil((locked_until - current).total_seconds()))
        failures = (
            1
            if row is None or (row["locked_until"] and locked_until <= current)
            else row["failures"] + 1
        )
        locked_until = (
            (current + datetime.timedelta(minutes=10)).isoformat()
            if failures >= 5
            else None
        )
        connection.execute(
            """
            INSERT INTO login_attempts (email, ip_address, failures, locked_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(email, ip_address) DO UPDATE SET
                failures = excluded.failures,
                locked_until = excluded.locked_until
            """,
            (email, ip_address, failures, locked_until),
        )
        connection.commit()
        return 600 if locked_until else 0


def clear_login_failures(email, ip_address):
    with closing(get_db_connection()) as connection:
        connection.execute(
            "DELETE FROM login_attempts WHERE email = ? AND ip_address = ?",
            (email, ip_address),
        )
        connection.commit()


def update_user_profile(user_id, full_name, address, contact_number):
    with closing(get_db_connection()) as connection:
        connection.execute(
            """
            UPDATE users SET full_name = ?, saved_address = ?,
                saved_contact_number = ? WHERE id = ?
            """,
            (full_name, address, contact_number, user_id),
        )
        connection.commit()


def change_user_password(user_id, password_hash):
    with closing(get_db_connection()) as connection:
        cursor = connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
        connection.commit()
        return cursor.rowcount > 0


def reset_customer_password(user_id, password_hash):
    with closing(get_db_connection()) as connection:
        cursor = connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ? AND role = 'customer'",
            (password_hash, user_id),
        )
        connection.commit()
        return cursor.rowcount > 0


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
        connection.execute("BEGIN IMMEDIATE")
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
        connection.execute(
            """
            INSERT INTO order_events (order_id, status, changed_at, changed_by)
            VALUES (?, 'Pending', ?, ?)
            """,
            (cursor.lastrowid, order["created_at"], order["customer_name"]),
        )
        connection.commit()
        return cursor.lastrowid


def get_orders_for_user(user_id):
    with closing(get_db_connection()) as connection:
        orders = connection.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()
        return _attach_order_events(connection, orders)


def _attach_order_events(connection, orders):
    result = [dict(order) for order in orders]
    if not result:
        return result
    order_ids = [order["id"] for order in result]
    placeholders = ",".join("?" for _ in order_ids)
    events = connection.execute(
        f"""
        SELECT order_id, status, changed_at, changed_by FROM order_events
        WHERE order_id IN ({placeholders}) ORDER BY id
        """,
        order_ids,
    ).fetchall()
    by_order = {order_id: [] for order_id in order_ids}
    for event in events:
        by_order[event["order_id"]].append(dict(event))
    for order in result:
        order["events"] = by_order[order["id"]]
    return result


def get_order_for_user(order_id, user_id):
    with closing(get_db_connection()) as connection:
        order = connection.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?",
            (order_id, user_id),
        ).fetchone()
        return _attach_order_events(connection, [order])[0] if order else None


def get_admin_order(order_id):
    with closing(get_db_connection()) as connection:
        order = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        return _attach_order_events(connection, [order])[0] if order else None


def get_admin_users():
    with closing(get_db_connection()) as connection:
        return connection.execute(
            """
            SELECT id, full_name, email, role FROM users
            WHERE role = 'customer' ORDER BY full_name COLLATE NOCASE
            """
        ).fetchall()


def get_admin_orders(status=None, search=None, page=1, per_page=10, sort="newest"):
    filters = []
    parameters = []
    if status and status != "All":
        filters.append("status = ?")
        parameters.append(status)
    if search:
        escaped_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append("(customer_name LIKE ? ESCAPE '\\' OR CAST(id AS TEXT) = ?)")
        parameters.extend((f"%{escaped_search}%", search.lstrip("#")))
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    with closing(get_db_connection()) as connection:
        stats = connection.execute(
            """
            SELECT COUNT(*) AS total_orders,
                   SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending_count,
                   COALESCE(SUM(CASE WHEN status != 'Cancelled' AND payment_status = 'Paid' THEN total_price ELSE 0 END), 0) AS revenue,
                   SUM(CASE WHEN pickup_date = ? AND status != 'Cancelled' THEN 1 ELSE 0 END) AS pickups_today,
                   COALESCE(SUM(CASE WHEN status != 'Cancelled' AND payment_status = 'Unpaid' THEN total_price ELSE 0 END), 0) AS unpaid_amount
            FROM orders
            """,
            (datetime.datetime.now(MANILA_TZ).date().isoformat(),),
        ).fetchone()
        total_matching = connection.execute(
            f"SELECT COUNT(*) FROM orders {where_clause}", parameters
        ).fetchone()[0]
        offset = (page - 1) * per_page
        order_sort = "pickup_date ASC, pickup_time ASC, id DESC" if sort == "pickup" else "id DESC"
        orders = connection.execute(
            f"""
            SELECT * FROM orders {where_clause}
            ORDER BY {order_sort} LIMIT ? OFFSET ?
            """,
            (*parameters, per_page, offset),
        ).fetchall()
        orders = _attach_order_events(connection, orders)
    return orders, stats, total_matching


def update_order_status(order_id, new_status, changed_by, changed_at):
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
        connection.execute(
            """
            INSERT INTO order_events (order_id, status, changed_at, changed_by)
            VALUES (?, ?, ?, ?)
            """,
            (order_id, new_status, changed_at, changed_by),
        )
        connection.commit()
        return "updated"


def mark_order_paid(order_id, payment_reference, paid_at):
    with closing(get_db_connection()) as connection:
        cursor = connection.execute(
            """
            UPDATE orders SET payment_status = 'Paid', payment_reference = ?,
                paid_at = ?
            WHERE id = ? AND payment_status = 'Unpaid' AND status != 'Cancelled'
                AND actual_weight_kg IS NOT NULL
            """,
            (payment_reference, paid_at, order_id),
        )
        connection.commit()
        return cursor.rowcount > 0


def cancel_pending_order(order_id, user_id, changed_at, changed_by):
    with closing(get_db_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE orders SET status = 'Cancelled'
            WHERE id = ? AND user_id = ? AND status = 'Pending'
            """,
            (order_id, user_id),
        )
        if cursor.rowcount:
            connection.execute(
                """
                INSERT INTO order_events (order_id, status, changed_at, changed_by)
                VALUES (?, 'Cancelled', ?, ?)
                """,
                (order_id, changed_at, changed_by),
            )
        connection.commit()
        return cursor.rowcount > 0


def update_actual_weight(order_id, actual_weight_kg, total_price):
    with closing(get_db_connection()) as connection:
        cursor = connection.execute(
            """
            UPDATE orders SET actual_weight_kg = ?, total_price = ?
            WHERE id = ? AND status != 'Cancelled' AND payment_status = 'Unpaid'
            """,
            (actual_weight_kg, total_price, order_id),
        )
        connection.commit()
        return cursor.rowcount > 0
