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
    create_order,
    create_user,
    get_admin_orders,
    get_db_connection,
    get_orders_for_user,
    get_order_for_user,
    get_user_by_email,
    get_user_by_id,
    init_db,
    mark_order_paid,
    provision_admin,
    restore_orders_from_backup,
    update_order_status,
)
from laundry_logic import (
    MAX_PICKUP_DAYS,
    MAX_WEIGHT_KG,
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
        "today": datetime.date.today().isoformat(),
    }


@app.before_request
def enforce_csrf_and_load_user():
    if request.method == "POST":
        submitted_token = request.form.get("csrf_token", "")
        expected_token = session.get("_csrf_token", "")
        if not expected_token or not hmac.compare_digest(
            submitted_token, expected_token
        ):
            abort(400, description="The form expired or its security token was invalid. Please try again.")

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


@app.route("/")
def index():
    return render_template(
        "index.html",
        pickup_min=datetime.date.today().isoformat(),
        pickup_max=(
            datetime.date.today() + datetime.timedelta(days=MAX_PICKUP_DAYS)
        ).isoformat(),
    )


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_user_by_email(email)
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Email or password is incorrect.", "danger")
            return render_template("login.html"), 401
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
            return render_template("register.html"), 400
        if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
            flash("Enter a valid email address.", "danger")
            return render_template("register.html"), 400
        if len(password) < 8 or len(password) > 128:
            flash("Password must be between 8 and 128 characters.", "danger")
            return render_template("register.html"), 400
        try:
            user_id = create_user(
                full_name, email, generate_password_hash(password), role="customer"
            )
        except sqlite3.IntegrityError:
            flash("An account with that email already exists.", "danger")
            return render_template("register.html"), 409
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
                    "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
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
    return render_template(
        "customer_dashboard.html",
        orders=orders,
        pickup_min=datetime.date.today().isoformat(),
        pickup_max=(datetime.date.today() + datetime.timedelta(days=MAX_PICKUP_DAYS)).isoformat(),
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
    orders, stats, total_matching = get_admin_orders(
        status, search, requested_page, PAGINATION_SIZE
    )
    total_pages = max(1, math.ceil(total_matching / PAGINATION_SIZE))
    page = min(requested_page, total_pages)
    if requested_page > total_pages:
        return redirect(url_for("admin_dashboard", status=status, q=search, page=total_pages))
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
    )


@app.route("/update_status/<int:order_id>", methods=["POST"])
@role_required("admin")
def update_status(order_id):
    new_status = request.form.get("status", "").strip()
    if new_status not in ADMIN_STATUSES:
        flash("Please select a valid order status.", "danger")
        return redirect(url_for("admin_dashboard"))
    result = update_order_status(order_id, new_status)
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
    if not mark_order_paid(order_id):
        flash("Order not found or already marked as paid.", "info")
    elif not refresh_json_backup():
        flash("Payment updated, but the JSON backup could not be updated.", "warning")
    else:
        flash("Payment marked as paid.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/cancel_order/<int:order_id>", methods=["POST"])
@role_required("customer")
def cancel_order(order_id):
    if not cancel_pending_order(order_id, g.current_user["id"]):
        flash("Only your pending orders can be cancelled.", "danger")
    elif not refresh_json_backup():
        flash("Order cancelled, but the JSON backup could not be updated.", "warning")
    else:
        flash("Your order has been cancelled.", "success")
    return redirect(url_for("customer_dashboard"))


@app.route("/receipt/<int:order_id>")
def receipt(order_id):
    if g.current_user is None:
        return redirect(url_for("login_page"))
    if g.current_user["role"] == "admin":
        with closing(get_db_connection()) as connection:
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
    else:
        order = get_order_for_user(order_id, g.current_user["id"])
    if order is None:
        abort(404)
    try:
        breakdown = get_price_breakdown(order["service_type"], order["weight_kg"])
    except ValueError:
        legacy_weight_charge = min(
            float(order["weight_kg"]) * PER_KG_RATE, float(order["total_price"])
        )
        breakdown = {
            "base_price": max(float(order["total_price"]) - legacy_weight_charge, 0),
            "weight_charge": legacy_weight_charge,
            "total_price": float(order["total_price"]),
        }
    return render_template("receipt.html", order=order, breakdown=breakdown)


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
