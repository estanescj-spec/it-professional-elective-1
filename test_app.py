import datetime
import os
import re
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

_TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="freshnest-tests-")
_TEST_ROOT = Path(_TEST_DIRECTORY.name)
os.environ["FRESHNEST_DB_PATH"] = str(_TEST_ROOT / "freshnest.db")
os.environ["FRESHNEST_BACKUP_PATH"] = str(_TEST_ROOT / "backup_orders.json")
os.environ["FRESHNEST_SECRET_KEY"] = "isolated-test-secret-key"
os.environ["FRESHNEST_ADMIN_EMAIL"] = "admin@test.example"
os.environ["FRESHNEST_ADMIN_PASSWORD"] = "integration-test-admin-password"
os.environ["FRESHNEST_ADMIN_NAME"] = "Test Administrator"

import app as freshnest
import db
from werkzeug.security import check_password_hash

freshnest.app.config.update(TESTING=True)


def csrf_token(response):
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    if not match:
        raise AssertionError("Rendered form did not include a CSRF token.")
    return match.group(1).decode()


def register_customer(client, full_name, email):
    response = client.get("/register")
    result = client.post(
        "/register",
        data={
            "csrf_token": csrf_token(response),
            "full_name": full_name,
            "email": email,
            "password": "customer-test-password",
            "confirm_password": "customer-test-password",
        },
    )
    if result.status_code != 302:
        raise AssertionError(f"Customer registration failed: {result.status_code}")
    return freshnest.get_user_by_email(email)


class FreshNestClientTests(unittest.TestCase):
    def setUp(self):
        self.client = freshnest.app.test_client()

    def test_csrf_rejection_and_registration_preserve_form_values(self):
        self.client.get("/login")
        login_rejected = self.client.post(
            "/login",
            data={"email": "admin@test.example", "password": "wrong"},
        )
        self.assertEqual(login_rejected.status_code, 400)

        rejected = self.client.post(
            "/register",
            data={"full_name": "Test Name", "email": "no-token@example.test"},
        )
        self.assertEqual(rejected.status_code, 400)

        page = self.client.get("/register")
        response = self.client.post(
            "/register",
            data={
                "csrf_token": csrf_token(page),
                "full_name": "Test Name",
                "email": "mismatch@example.test",
                "password": "password-one",
                "confirm_password": "password-two",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'value="Test Name"', response.data)
        self.assertIn(b'value="mismatch@example.test"', response.data)

    def test_password_login_and_failed_attempt_lockout(self):
        page = self.client.get("/login")
        response = self.client.post(
            "/login",
            data={
                "csrf_token": csrf_token(page),
                "email": "admin@test.example",
                "password": "integration-test-admin-password",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin")
        user = freshnest.get_user_by_email("admin@test.example")
        self.assertTrue(
            check_password_hash(
                user["password_hash"], "integration-test-admin-password"
            )
        )

        locked_client = freshnest.app.test_client()
        login_page = locked_client.get("/login")
        token = csrf_token(login_page)
        for _ in range(5):
            response = locked_client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "email": "guessing@example.test",
                    "password": "wrong-password",
                },
            )
            self.assertEqual(response.status_code, 401)
        response = locked_client.post(
            "/login",
            data={
                "csrf_token": token,
                "email": "guessing@example.test",
                "password": "wrong-password",
            },
        )
        self.assertEqual(response.status_code, 429)

    def test_another_customer_cannot_open_receipt(self):
        owner = freshnest.app.test_client()
        owner_user = register_customer(owner, "Receipt Owner", "owner@example.test")
        owner_page = owner.get("/customer")
        pickup_date = datetime.datetime.now(freshnest.MANILA_TZ).date() + datetime.timedelta(days=1)
        while pickup_date.weekday() == 6:
            pickup_date += datetime.timedelta(days=1)
        owner.post(
            "/customer",
            data={
                "csrf_token": csrf_token(owner_page),
                "service_type": "Wash & Fold",
                "weight_kg": "2",
                "payment_method": "Cash on Delivery",
                "pickup_date": pickup_date.isoformat(),
                "pickup_time": "11:00",
                "address": "Private Address",
                "contact_number": "09171234567",
            },
        )
        order_id = freshnest.get_orders_for_user(owner_user["id"])[0]["id"]
        stranger = freshnest.app.test_client()
        register_customer(stranger, "Other Customer", "other@example.test")
        self.assertEqual(stranger.get(f"/receipt/{order_id}").status_code, 404)
        self.assertEqual(owner.get(f"/receipt/{order_id}").status_code, 200)

    def test_status_transition_and_customer_cancellation(self):
        owner = freshnest.app.test_client()
        user = register_customer(owner, "Order Owner", "status-owner@example.test")
        page = owner.get("/customer")
        pickup_date = datetime.datetime.now(freshnest.MANILA_TZ).date() + datetime.timedelta(days=1)
        while pickup_date.weekday() == 6:
            pickup_date += datetime.timedelta(days=1)
        owner.post(
            "/customer",
            data={
                "csrf_token": csrf_token(page),
                "service_type": "Wash & Fold",
                "weight_kg": "2",
                "payment_method": "Cash on Delivery",
                "pickup_date": pickup_date.isoformat(),
                "pickup_time": "11:00",
                "address": "Order Street",
                "contact_number": "09171234567",
            },
        )
        order_id = freshnest.get_orders_for_user(user["id"])[0]["id"]
        admin = freshnest.app.test_client()
        login = admin.get("/login")
        admin.post(
            "/login",
            data={
                "csrf_token": csrf_token(login),
                "email": "admin@test.example",
                "password": "integration-test-admin-password",
            },
        )
        admin_page = admin.get("/admin")
        bad_transition = admin.post(
            f"/update_status/{order_id}",
            data={"csrf_token": csrf_token(admin_page), "status": "Washing"},
        )
        self.assertEqual(bad_transition.status_code, 302)
        self.assertEqual(
            freshnest.get_orders_for_user(user["id"])[0]["status"], "Pending"
        )

        owner_page = owner.get("/customer")
        cancelled = owner.post(
            f"/cancel_order/{order_id}",
            data={"csrf_token": csrf_token(owner_page)},
        )
        self.assertEqual(cancelled.status_code, 302)
        order = freshnest.get_orders_for_user(user["id"])[0]
        self.assertEqual(order["status"], "Cancelled")
        self.assertEqual(order["events"][-1]["changed_by"], "Order Owner")

    def test_actual_weight_required_and_payment_reference_saved(self):
        customer = freshnest.app.test_client()
        user = register_customer(customer, "Payment Customer", "payment@example.test")
        page = customer.get("/customer")
        pickup_date = datetime.datetime.now(freshnest.MANILA_TZ).date() + datetime.timedelta(days=1)
        while pickup_date.weekday() == 6:
            pickup_date += datetime.timedelta(days=1)
        customer.post(
            "/customer",
            data={
                "csrf_token": csrf_token(page),
                "service_type": "Wash & Fold",
                "weight_kg": "2",
                "payment_method": "GCash",
                "pickup_date": pickup_date.isoformat(),
                "pickup_time": "11:00",
                "address": "Payment Street",
                "contact_number": "09171234567",
            },
        )
        order = freshnest.get_orders_for_user(user["id"])[0]
        admin = freshnest.app.test_client()
        login = admin.get("/login")
        admin.post(
            "/login",
            data={
                "csrf_token": csrf_token(login),
                "email": "admin@test.example",
                "password": "integration-test-admin-password",
            },
        )
        admin_page = admin.get("/admin")
        pending_payment = admin.post(
            f"/mark_paid/{order['id']}",
            data={"csrf_token": csrf_token(admin_page), "payment_reference": "TXN-1"},
        )
        self.assertEqual(pending_payment.status_code, 302)
        self.assertEqual(
            freshnest.get_orders_for_user(user["id"])[0]["payment_status"], "Unpaid"
        )
        admin_page = admin.get("/admin")
        saved_weight = admin.post(
            f"/update_weight/{order['id']}",
            data={
                "csrf_token": csrf_token(admin_page),
                "actual_weight_kg": "3",
            },
        )
        self.assertEqual(saved_weight.status_code, 302)
        order = freshnest.get_orders_for_user(user["id"])[0]
        self.assertEqual(order["actual_weight_kg"], 3.0)
        self.assertEqual(order["total_price"], 240.0)
        admin_page = admin.get("/admin")
        no_reference = admin.post(
            f"/mark_paid/{order['id']}",
            data={"csrf_token": csrf_token(admin_page)},
        )
        self.assertEqual(no_reference.status_code, 302)
        admin_page = admin.get("/admin")
        paid = admin.post(
            f"/mark_paid/{order['id']}",
            data={
                "csrf_token": csrf_token(admin_page),
                "payment_reference": "GCASH-DEMO-123",
            },
        )
        self.assertEqual(paid.status_code, 302)
        order = freshnest.get_orders_for_user(user["id"])[0]
        self.assertEqual(order["payment_status"], "Paid")
        self.assertEqual(order["payment_reference"], "GCASH-DEMO-123")
        self.assertTrue(order["paid_at"])
        self.assertFalse(
            db.update_actual_weight(order["id"], 4, 260)
        )

    def test_static_file_does_not_query_current_user(self):
        with patch("app.get_user_by_id", side_effect=AssertionError("unexpected query")):
            response = self.client.get("/static/js/script.js")
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_customer_can_update_profile_and_change_password(self):
        customer = freshnest.app.test_client()
        user = register_customer(
            customer, "Profile Customer", "profile@example.test"
        )
        dashboard = customer.get("/customer")
        updated = customer.post(
            "/account/profile",
            data={
                "csrf_token": csrf_token(dashboard),
                "full_name": "Updated Customer",
                "saved_address": "Updated Address",
                "saved_contact_number": "09171234567",
            },
        )
        self.assertEqual(updated.status_code, 302)
        saved_user = freshnest.get_user_by_id(user["id"])
        self.assertEqual(saved_user["full_name"], "Updated Customer")
        self.assertEqual(saved_user["saved_address"], "Updated Address")
        self.assertEqual(saved_user["saved_contact_number"], "09171234567")

        dashboard = customer.get("/customer")
        changed = customer.post(
            "/account/password",
            data={
                "csrf_token": csrf_token(dashboard),
                "current_password": "customer-test-password",
                "new_password": "new-customer-test-password",
                "confirm_password": "new-customer-test-password",
            },
        )
        self.assertEqual(changed.status_code, 302)
        customer.post("/logout", data={"csrf_token": csrf_token(customer.get("/"))})
        login_page = customer.get("/login")
        logged_in = customer.post(
            "/login",
            data={
                "csrf_token": csrf_token(login_page),
                "email": "profile@example.test",
                "password": "new-customer-test-password",
            },
        )
        self.assertEqual(logged_in.status_code, 302)
        self.assertEqual(logged_in.headers["Location"], "/customer")

    def test_profile_page_is_next_to_bookings_and_contains_account_forms(self):
        customer = freshnest.app.test_client()
        register_customer(customer, "Profile Page Customer", "profile-page@example.test")
        response = customer.get("/profile")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b">Profile</a>", response.data)
        self.assertIn(b"Profile details", response.data)
        self.assertIn(b"Change password", response.data)
        self.assertIn(b"My bookings", response.data)

        bookings = customer.get("/customer")
        self.assertNotIn(b"Profile and password", bookings.data)
        self.assertNotIn(b"Change password", bookings.data)

    def test_admin_can_open_and_update_own_profile(self):
        admin = freshnest.app.test_client()
        login = admin.get("/login")
        signed_in = admin.post(
            "/login",
            data={
                "csrf_token": csrf_token(login),
                "email": "admin@test.example",
                "password": "integration-test-admin-password",
            },
        )
        self.assertEqual(signed_in.status_code, 302)

        profile_page = admin.get("/profile")
        self.assertEqual(profile_page.status_code, 200)
        self.assertIn(b">Admin panel</a>", profile_page.data)
        self.assertIn(b">Profile</a>", profile_page.data)
        self.assertIn(b"Change password", profile_page.data)
        dashboard = admin.get("/admin")
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn(b"Change your admin password", dashboard.data)
        changed = admin.post(
            "/account/profile",
            data={
                "csrf_token": csrf_token(profile_page),
                "full_name": "Updated Administrator",
                "saved_address": "Admin Office",
                "saved_contact_number": "09171234567",
            },
        )
        self.assertEqual(changed.status_code, 302)
        self.assertEqual(changed.headers["Location"], "/profile")
        updated_admin = freshnest.get_user_by_email("admin@test.example")
        self.assertEqual(updated_admin["full_name"], "Updated Administrator")
        self.assertEqual(updated_admin["saved_address"], "Admin Office")

    def test_home_and_database_journal_mode(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"7:00 AM", home.data)
        self.assertIn(b"8:00 PM", home.data)
        with closing(db.get_db_connection()) as connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_admin_search_escapes_like_wildcards_and_pickup_sort_is_available(self):
        get_admin_orders = db.get_admin_orders
        orders, _, total = get_admin_orders(search="%")
        self.assertEqual(total, 0)
        self.assertEqual(orders, [])
        orders, _, _ = get_admin_orders(sort="pickup")
        self.assertEqual(
            list(orders),
            sorted(orders, key=lambda order: (order["pickup_date"], order["pickup_time"], -order["id"])),
        )

    def test_restore_preserves_user_link_when_account_is_present(self):
        previous_path = db.DB_PATH
        try:
            db.DB_PATH = _TEST_ROOT / "restore.db"
            db.init_db()
            user_id = db.create_user(
                "Restore Owner",
                "restore@example.test",
                "test-hash",
                role="customer",
            )
            db.restore_orders_from_backup(
                [
                    {
                        "id": 411,
                        "user_id": user_id,
                        "customer_name": "Restore Owner",
                        "service_type": "Wash & Fold",
                        "weight_kg": 2,
                        "payment_method": "Cash on Delivery",
                        "total_price": 220,
                        "pickup_date": "2026-10-20",
                        "created_at": "2026-10-13T10:00:00+08:00",
                    }
                ]
            )
            restored = db.get_orders_for_user(user_id)
            self.assertEqual(restored[0]["id"], 411)
            self.assertEqual(restored[0]["user_id"], user_id)
        finally:
            db.DB_PATH = previous_path


if __name__ == "__main__":
    unittest.main()
