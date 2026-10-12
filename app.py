import datetime
import hmac
import math
import os
import re
import secrets
import sqlite3
from contextlib import closing
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from db import (
    cancel_pending_order,
    change_user_password,
    clear_login_failures,
    create_order,
    create_user,
    get_admin_users,
    get_admin_order,
    get_admin_orders,
    get_db_connection,
    get_login_lock_seconds,
    get_orders_for_user,
    get_order_for_user,
    get_user_by_email,
    get_user_by_id,
    init_db,
    mark_order_paid,
    provision_admin,
    record_login_failure,
    restore_orders_from_backup,
    reset_customer_password,
    update_actual_weight,
    update_user_profile,
    update_order_status,
)
from laundry_logic import (
    MAX_PICKUP_DAYS,
    MAX_WEIGHT_KG,
    MANILA_TZ,
    ORDER_STATUSES,
    PAYMENT_METHODS,
    PER_KG_RATE,
    SERVICE_DESCRIPTIONS,
    SERVICE_RATES,
    STATUS_TRANSITIONS,
    SUNDAY_PICKUP_SLOTS,
    WEEKDAY_PICKUP_SLOTS,
    get_price_breakdown,
    load_json_backup,
    sync_backup,
    validate_pickup_date,
)

app = Flask(__name__)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
app.config.update(
    SECRET_KEY=os.environ.get("FRESHNEST_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FRESHNEST_HTTPS_ONLY", "").lower()
    in {"1", "true", "yes"},
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
)

init_db()
restore_orders_from_backup(load_json_backup())

admin_settings = {
    "email": os.environ.get("FRESHNEST_ADMIN_EMAIL", "").strip().lower(),
    "password": os.environ.get("FRESHNEST_ADMIN_PASSWORD", ""),
    "name": os.environ.get("FRESHNEST_ADMIN_NAME", "FreshNest Admin").strip(),
}
if bool(admin_settings["email"]) != bool(admin_settings["password"]):
    raise RuntimeError(
        "Set both FRESHNEST_ADMIN_EMAIL and FRESHNEST_ADMIN_PASSWORD to provision the initial administrator."
    )
if admin_settings["email"] and admin_settings["password"]:
    if len(admin_settings["password"]) < 12:
        raise RuntimeError("FRESHNEST_ADMIN_PASSWORD must be at least 12 characters.")
    if not EMAIL_PATTERN.fullmatch(admin_settings["email"]):
        raise RuntimeError("FRESHNEST_ADMIN_EMAIL must be a valid email address.")
    provision_admin(
        admin_settings["name"] or "FreshNest Admin",
        admin_settings["email"],
        admin_settings["password"],
    )
else:
    print(
        "Warning: No administrator provisioned. Set FRESHNEST_ADMIN_EMAIL and "
        "FRESHNEST_ADMIN_PASSWORD before logging in as an administrator."
    )
if not os.environ.get("FRESHNEST_SECRET_KEY"):
    print(
        "Warning: FRESHNEST_SECRET_KEY is unset; sessions will expire when the app restarts."
    )

PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9\s()-]{6,29}$")
SHOP_ADDRESS = "Blk 12, Lot 8, Barangay San Isidro, Antipolo City, Rizal 1870"
SHOP_PHONE = "0917 123 4567"
ADMIN_STATUSES = ORDER_STATUSES
PAGINATION_SIZE = 10


def csrf_token():
    token = session.get("_csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_template_settings():
    today = datetime.datetime.now(MANILA_TZ).date()
    return {
        "csrf_token": csrf_token,
        "service_rates": SERVICE_RATES,
        "service_descriptions": SERVICE_DESCRIPTIONS,
        "per_kg_rate": PER_KG_RATE,
        "payment_methods": PAYMENT_METHODS,
        "max_weight_kg": MAX_WEIGHT_KG,
        "max_pickup_days": MAX_PICKUP_DAYS,
        "weekday_pickup_slots": WEEKDAY_PICKUP_SLOTS,
        "sunday_pickup_slots": SUNDAY_PICKUP_SLOTS,
        "today": today.isoformat(),
        "current_time": datetime.datetime.now(MANILA_TZ).strftime("%H:%M"),
        "operating_weekday_hours": pickup_hours(WEEKDAY_PICKUP_SLOTS),
        "operating_sunday_hours": pickup_hours(SUNDAY_PICKUP_SLOTS),
        "shop_address": SHOP_ADDRESS,
        "shop_phone": SHOP_PHONE,
    }


def pickup_hours(slots):
    start = format_clock(slots[0])
    end_hour = int(slots[-1][:2]) + 1
    end = format_clock(f"{end_hour:02d}:00")
    return f"{start} – {end}"


def format_clock(value):
    parsed = datetime.datetime.strptime(value, "%H:%M")
    return f"{parsed.hour % 12 or 12}:{parsed.minute:02d} {'AM' if parsed.hour < 12 else 'PM'}"


@app.template_filter("display_datetime")
def display_datetime(value):
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MANILA_TZ)
    local = parsed.astimezone(MANILA_TZ)
    return f"{local.strftime('%b')} {local.day}, {local.year}, {format_clock(local.strftime('%H:%M'))}"


@app.template_filter("display_pickup_time")
def display_pickup_time(value):
    try:
        return format_clock(value)
    except (TypeError, ValueError):
        return value


@app.before_request
def enforce_csrf_and_load_user():
    if request.method == "POST":
        submitted_token = request.form.get("csrf_token", "")
        expected_token = session.get("_csrf_token", "")
        if not expected_token or not hmac.compare_digest(
            submitted_token, expected_token
        ):
            abort(400, description="The form expired or its security token was invalid. Please try again.")

    if request.endpoint == "static":
        g.current_user = None
        return
    user_id = session.get("user_id")
    g.current_user = get_user_by_id(user_id) if user_id else None
    if user_id and g.current_user is None:
        session.clear()


def role_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if g.current_user is None or g.current_user["role"] != role:
                flash("Please sign in with an authorized account.", "warning")
                return redirect(url_for("login_page"))
            return view(*args, **kwargs)

        return wrapped_view

    return decorator


def refresh_json_backup():
    with closing(get_db_connection()) as connection:
        return sync_backup(connection)


def book_form_redirect():
    return redirect(url_for("customer_dashboard") + "#booking-form-title")


def manila_timestamp():
    return datetime.datetime.now(MANILA_TZ).isoformat(timespec="seconds")


@app.route("/")
def index():
    today = datetime.datetime.now(MANILA_TZ).date()
    return render_template(
        "index.html",
        pickup_min=today.isoformat(),
        pickup_max=(today + datetime.timedelta(days=MAX_PICKUP_DAYS)).isoformat(),
    )


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        ip_address = request.remote_addr or "unknown"
        lock_seconds = get_login_lock_seconds(email, ip_address)
        if lock_seconds:
            flash("Too many failed sign-in attempts. Please try again in 10 minutes.", "danger")
            return render_template("login.html", email=email), 429
        user = get_user_by_email(email)
        if user is None or not check_password_hash(user["password_hash"], password):
            record_login_failure(email, ip_address)
            flash("Email or password is incorrect.", "danger")
            return render_template("login.html", email=email), 401
        clear_login_failures(email, ip_address)
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["role"] = user["role"]
        session["user_name"] = user["full_name"]
        flash(f"Welcome back, {user['full_name']}.", "success")
        destination = "admin_dashboard" if user["role"] == "admin" else "customer_dashboard"
        return redirect(url_for(destination))
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not full_name or len(full_name) > 80:
            flash("Enter your name (up to 80 characters).", "danger")
            return render_template("register.html", full_name=full_name, email=email), 400
        if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
            flash("Enter a valid email address.", "danger")
            return render_template("register.html", full_name=full_name, email=email), 400
        if len(password) < 8 or len(password) > 128:
            flash("Password must be between 8 and 128 characters.", "danger")
            return render_template("register.html", full_name=full_name, email=email), 400
        if password != request.form.get("confirm_password", ""):
            flash("The passwords do not match.", "danger")
            return render_template("register.html", full_name=full_name, email=email), 400
        try:
            user_id = create_user(
                full_name, email, generate_password_hash(password), role="customer"
            )
        except sqlite3.IntegrityError:
            flash("An account with that email already exists.", "danger")
            return render_template("register.html", full_name=full_name, email=email), 409
        session.clear()
        session.permanent = True
        session["user_id"] = user_id
        session["role"] = "customer"
        session["user_name"] = full_name
        flash("Your account is ready. Welcome to FreshNest!", "success")
        return redirect(url_for("customer_dashboard"))
    return render_template("register.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("index"))


@app.route("/account/profile", methods=["POST"])
def update_profile():
    if g.current_user is None or g.current_user["role"] not in {"customer", "admin"}:
        flash("Please sign in to update your profile.", "warning")
        return redirect(url_for("login_page"))
    full_name = request.form.get("full_name", "").strip()
    address = request.form.get("saved_address", "").strip()
    contact_number = request.form.get("saved_contact_number", "").strip()
    destination = url_for("profile")
    if not full_name or len(full_name) > 80:
        flash("Enter your name (up to 80 characters).", "danger")
        return redirect(destination)
    if address and len(address) > 300:
        flash("Saved address cannot exceed 300 characters.", "danger")
        return redirect(destination)
    if contact_number and not PHONE_PATTERN.fullmatch(contact_number):
        flash("Enter a valid contact number.", "danger")
        return redirect(destination)
    update_user_profile(g.current_user["id"], full_name, address, contact_number)
    session["user_name"] = full_name
    flash("Your profile was updated.", "success")
    return redirect(url_for("profile"))


@app.route("/account/password", methods=["POST"])
def change_password():
    if g.current_user is None:
        flash("Please sign in to change your password.", "warning")
        return redirect(url_for("login_page"))
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    user = get_user_by_id(g.current_user["id"])
    if user is None or not check_password_hash(user["password_hash"], current_password):
        flash("Your current password is incorrect.", "danger")
        return redirect(url_for("profile"))
    if len(new_password) < 8 or len(new_password) > 128:
        flash("New password must be between 8 and 128 characters.", "danger")
    elif new_password != confirm_password:
        flash("The new passwords do not match.", "danger")
    else:
        change_user_password(user["id"], generate_password_hash(new_password))
        flash("Your password has been changed.", "success")
    return redirect(url_for("profile"))


@app.route("/profile")
def profile():
    if g.current_user is None or g.current_user["role"] not in {"customer", "admin"}:
        flash("Please sign in to view your profile.", "warning")
        return redirect(url_for("login_page"))
    return render_template("profile.html")


@app.route("/customer", methods=["GET", "POST"])
@role_required("customer")
def customer_dashboard():
    if request.method == "POST":
        service_type = request.form.get("service_type", "")
        payment_method = request.form.get("payment_method", "")
        address = request.form.get("address", "").strip()
        contact_number = request.form.get("contact_number", "").strip()
        if payment_method not in PAYMENT_METHODS:
            flash("Please select a valid payment method.", "danger")
            return book_form_redirect()
        if not address or len(address) > 300:
            flash("Enter a pickup address (up to 300 characters).", "danger")
            return book_form_redirect()
        if not PHONE_PATTERN.fullmatch(contact_number):
            flash("Enter a valid contact number.", "danger")
            return book_form_redirect()
        update_user_profile(
            g.current_user["id"],
            g.current_user["full_name"],
            address,
            contact_number,
        )
        try:
            weight_kg = float(request.form.get("weight_kg", ""))
            breakdown = get_price_breakdown(service_type, weight_kg)
        except ValueError as error:
            flash(str(error), "danger")
            return book_form_redirect()

        pickup_date = request.form.get("pickup_date", "")
        pickup_time = request.form.get("pickup_time", "")
        valid_date, date_error = validate_pickup_date(pickup_date, pickup_time)
        if date_error:
            flash(date_error, "danger")
            return book_form_redirect()
        try:
            create_order(
                {
                    "user_id": g.current_user["id"],
                    "customer_name": g.current_user["full_name"],
                    "service_type": service_type,
                    "weight_kg": weight_kg,
                    "payment_method": payment_method,
                    "total_price": breakdown["total_price"],
                    "pickup_date": valid_date,
                    "pickup_time": pickup_time,
                    "address": address,
                    "contact_number": contact_number,
                    "created_at": datetime.datetime.now(MANILA_TZ).isoformat(timespec="seconds"),
                }
            )
        except sqlite3.Error:
            app.logger.exception("Could not create a laundry order")
            abort(500)
        if not refresh_json_backup():
            flash("Booking saved, but its JSON backup could not be updated.", "warning")
        else:
            flash("Your laundry pickup has been booked.", "success")
        return redirect(url_for("customer_dashboard"))

    orders = get_orders_for_user(g.current_user["id"])
    today = datetime.datetime.now(MANILA_TZ).date()
    return render_template(
        "customer_dashboard.html",
        orders=orders,
        pickup_min=today.isoformat(),
        pickup_max=(today + datetime.timedelta(days=MAX_PICKUP_DAYS)).isoformat(),
    )


@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    status = request.args.get("status", "All")
    search = request.args.get("q", "").strip()[:80]
    if status not in ("All", *ADMIN_STATUSES):
        status = "All"
    try:
        requested_page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        requested_page = 1
    sort = request.args.get("sort", "newest")
    if sort not in {"newest", "pickup"}:
        sort = "newest"
    orders, stats, total_matching = get_admin_orders(
        status, search, requested_page, PAGINATION_SIZE, sort=sort
    )
    total_pages = max(1, math.ceil(total_matching / PAGINATION_SIZE))
    page = min(requested_page, total_pages)
    if requested_page > total_pages:
        return redirect(
            url_for(
                "admin_dashboard",
                status=status,
                q=search,
                sort=sort,
                page=total_pages,
            )
        )
    return render_template(
        "admin_dashboard.html",
        orders=orders,
        stats=stats,
        status_filter=status,
        search=search,
        page=page,
        total_pages=total_pages,
        statuses=ADMIN_STATUSES,
        status_transitions=STATUS_TRANSITIONS,
        sort=sort,
        users=get_admin_users(),
    )


@app.route("/admin/reset_password/<int:user_id>", methods=["POST"])
@role_required("admin")
def admin_reset_password(user_id):
    password = request.form.get("new_password", "")
    if len(password) < 8 or len(password) > 128:
        flash("Temporary password must be between 8 and 128 characters.", "danger")
    elif not reset_customer_password(user_id, generate_password_hash(password)):
        flash("Customer account not found.", "danger")
    else:
        flash("Customer password reset. Share the temporary password securely.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/update_status/<int:order_id>", methods=["POST"])
@role_required("admin")
def update_status(order_id):
    new_status = request.form.get("status", "").strip()
    if new_status not in ADMIN_STATUSES:
        flash("Please select a valid order status.", "danger")
        return redirect(url_for("admin_dashboard"))
    result = update_order_status(
        order_id,
        new_status,
        g.current_user["full_name"],
        manila_timestamp(),
    )
    messages = {
        "updated": ("Order status updated.", "success"),
        "unchanged": ("The order already has that status.", "info"),
        "not_found": ("Order not found.", "danger"),
        "invalid_transition": ("That order status transition is not allowed.", "danger"),
    }
    message, category = messages[result]
    if result == "updated" and not refresh_json_backup():
        message, category = "Order status updated, but the JSON backup could not be updated.", "warning"
    flash(message, category)
    return redirect(url_for("admin_dashboard"))


@app.route("/mark_paid/<int:order_id>", methods=["POST"])
@role_required("admin")
def mark_paid(order_id):
    with closing(get_db_connection()) as connection:
        order = connection.execute(
            "SELECT payment_method FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
    if order is None:
        flash("Order not found.", "danger")
    elif len(request.form.get("payment_reference", "").strip()) > 100:
        flash("Payment reference cannot exceed 100 characters.", "danger")
    elif order["payment_method"] != "Cash on Delivery" and not request.form.get("payment_reference", "").strip():
        flash("Enter the GCash/card transaction reference before confirming payment.", "danger")
    elif not mark_order_paid(
        order_id,
        request.form.get("payment_reference", "").strip()[:100],
        manila_timestamp(),
    ):
        flash("Record the actual laundry weight before marking this order paid.", "warning")
    elif not refresh_json_backup():
        flash("Payment updated, but the JSON backup could not be updated.", "warning")
    else:
        flash("Payment marked as paid.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/cancel_order/<int:order_id>", methods=["POST"])
@role_required("customer")
def cancel_order(order_id):
    if not cancel_pending_order(
        order_id,
        g.current_user["id"],
        manila_timestamp(),
        g.current_user["full_name"],
    ):
        flash("Only your pending orders can be cancelled.", "danger")
    elif not refresh_json_backup():
        flash("Order cancelled, but the JSON backup could not be updated.", "warning")
    else:
        flash("Your order has been cancelled.", "success")
    return redirect(url_for("customer_dashboard"))


@app.route("/update_weight/<int:order_id>", methods=["POST"])
@role_required("admin")
def update_weight(order_id):
    try:
        actual_weight = float(request.form.get("actual_weight_kg", ""))
        order = get_admin_order(order_id)
        if order is None:
            flash("Order not found.", "danger")
            return redirect(url_for("admin_dashboard"))
        breakdown = get_price_breakdown(order["service_type"], actual_weight)
    except ValueError as error:
        flash(str(error), "danger")
        return redirect(url_for("admin_dashboard"))
    if not update_actual_weight(order_id, actual_weight, breakdown["total_price"]):
        flash("Order not found or cancelled; actual weight was not updated.", "danger")
    elif not refresh_json_backup():
        flash("Actual weight saved, but the JSON backup could not be updated.", "warning")
    else:
        flash("Actual weight and final price saved.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/receipt/<int:order_id>")
def receipt(order_id):
    if g.current_user is None:
        return redirect(url_for("login_page"))
    if g.current_user["role"] == "admin":
        order = get_admin_order(order_id)
    else:
        order = get_order_for_user(order_id, g.current_user["id"])
    if order is None:
        abort(404)
    try:
        receipt_weight = order["actual_weight_kg"] or order["weight_kg"]
        breakdown = get_price_breakdown(order["service_type"], receipt_weight)
    except ValueError:
        legacy_weight_charge = min(
            float(order["weight_kg"]) * PER_KG_RATE, float(order["total_price"])
        )
        breakdown = {
            "base_price": max(float(order["total_price"]) - legacy_weight_charge, 0),
            "weight_charge": legacy_weight_charge,
            "total_price": float(order["total_price"]),
        }
    return render_template(
        "receipt.html",
        order=order,
        breakdown=breakdown,
        receipt_weight=order["actual_weight_kg"] or order["weight_kg"],
        events=order["events"],
    )


@app.errorhandler(400)
def bad_request(error):
    return render_template("errors/400.html", error=error), 400


@app.errorhandler(404)
def not_found(error):
    return render_template("errors/404.html"), 404


@app.errorhandler(500)
def internal_server_error(error):
    app.logger.error("Internal server error: %s", error)
    return render_template("errors/500.html"), 500


if __name__ == "__main__":
    app.run(debug=False)
