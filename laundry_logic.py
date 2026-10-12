import datetime
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = Path(
    os.environ.get("FRESHNEST_BACKUP_PATH", BASE_DIR / "data" / "backup_orders.json")
).resolve()
SERVICE_RATES = {
    "Wash & Fold": 180.0,
    "Comforter Wash": 250.0,
    "Pickup & Delivery": 50.0,
    "Rush Service": 80.0,
}
SERVICE_DESCRIPTIONS = {
    "Wash & Fold": "Everyday laundry, washed, dried, and folded.",
    "Comforter Wash": "A deeper clean for comforters and blankets.",
    "Pickup & Delivery": "Door-to-door convenience for your laundry.",
    "Rush Service": "For when you need your laundry back sooner.",
}
PER_KG_RATE = 20.0
MAX_WEIGHT_KG = 50.0
MAX_PICKUP_DAYS = 30
WEEKDAY_PICKUP_SLOTS = tuple(f"{hour:02d}:00" for hour in range(7, 20))
SUNDAY_PICKUP_SLOTS = tuple(f"{hour:02d}:00" for hour in range(8, 18))
PAYMENT_METHODS = ("Cash on Delivery", "GCash", "Credit/Debit Card")
ORDER_STATUSES = (
    "Pending",
    "Picked Up",
    "Washing",
    "Out for Delivery",
    "Completed",
    "Cancelled",
)
STATUS_TRANSITIONS = {
    "Pending": ("Picked Up", "Cancelled"),
    "Picked Up": ("Washing",),
    "Washing": ("Out for Delivery",),
    "Out for Delivery": ("Completed",),
    "Completed": (),
    "Cancelled": (),
}


def load_json_backup(path=None):
    """Load backup orders; missing, unreadable, or malformed files return an empty list."""
    backup_file = Path(path) if path is not None else DATA_FILE
    try:
        with backup_file.open("r", encoding="utf-8") as backup:
            content = backup.read().strip()
        if not content:
            return []
        orders = json.loads(content)
        if not isinstance(orders, list):
            raise ValueError("JSON backup must contain a list of orders.")
        return orders
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as error:
        print(f"Warning: Could not read JSON backup ({error}).")
        return []


def save_json_backup(orders_list, path=None):
    """Atomically save orders, preserving each previous backup with a timestamp."""
    backup_file = Path(path) if path is not None else DATA_FILE
    temporary_path = None
    try:
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        if backup_file.exists():
            backup_dir = backup_file.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shutil.copy2(
                backup_file,
                backup_dir / f"{backup_file.stem}_{timestamp}{backup_file.suffix}",
            )

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=backup_file.parent,
            prefix=f".{backup_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(orders_list, temporary, indent=2, ensure_ascii=False, default=str)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, backup_file)
        return True
    except (OSError, TypeError, ValueError) as error:
        print(f"Error saving JSON backup: {error}")
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                print(f"Warning: Could not remove temporary backup ({cleanup_error}).")
        return False


def sync_backup(connection):
    """Write the database's complete order list to the JSON backup."""
    rows = connection.execute("SELECT * FROM orders ORDER BY id").fetchall()
    return save_json_backup([dict(row) for row in rows])


def validate_pickup_date(date_str, pickup_time=None, today=None, now=None):
    """Validate pickup date range and, when provided, its operating-hour time slot."""
    if not isinstance(date_str, str) or not date_str.strip():
        return None, "Pickup date is required."
    try:
        pickup_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None, "Please enter a valid pickup date."
    if pickup_date.isoformat() != date_str:
        return None, "Please enter a valid pickup date."

    current_date = today or datetime.date.today()
    if pickup_date < current_date:
        return None, "Pickup date cannot be in the past."
    if pickup_date > current_date + datetime.timedelta(days=MAX_PICKUP_DAYS):
        return None, f"Pickup date must be within {MAX_PICKUP_DAYS} days."

    if pickup_time is not None:
        available_slots = (
            SUNDAY_PICKUP_SLOTS
            if pickup_date.weekday() == 6
            else WEEKDAY_PICKUP_SLOTS
        )
        if pickup_time not in available_slots:
            return None, "That pickup time is outside our operating hours for the selected day."
        if pickup_date == current_date:
            scheduled_time = datetime.datetime.strptime(pickup_time, "%H:%M").time()
            current_time = now or datetime.datetime.now().time()
            if scheduled_time <= current_time:
                return None, "Choose a pickup time that has not already passed."
    return pickup_date.isoformat(), None


def get_price_breakdown(service_type, weight_kg):
    """Return the service base, per-kilogram charge, and validated total."""
    try:
        weight = float(weight_kg)
    except (TypeError, ValueError) as error:
        raise ValueError("Weight must be a valid number.") from error
    if not math.isfinite(weight) or weight <= 0:
        raise ValueError("Weight must be greater than 0.")
    if weight > MAX_WEIGHT_KG:
        raise ValueError(f"Weight cannot exceed {MAX_WEIGHT_KG:g} kg.")
    try:
        base_price = SERVICE_RATES[service_type]
    except (KeyError, TypeError) as error:
        raise ValueError("Please select a valid service.") from error

    weight_charge = round(weight * PER_KG_RATE, 2)
    return {
        "base_price": base_price,
        "weight_charge": weight_charge,
        "total_price": round(base_price + weight_charge, 2),
    }


def calculate_price(service_type, weight_kg):
    """Calculate an order total, rejecting unsupported services and weights."""
    return get_price_breakdown(service_type, weight_kg)["total_price"]
