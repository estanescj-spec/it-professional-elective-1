# FreshNest Laundry

FreshNest is a small Flask laundry booking app. Customers can create accounts,
schedule pickups, see their order history, cancel pending orders, and print
receipts. Administrators can manage order progress and record payments.

## Features

- Customer registration and password-based sign-in using Werkzeug password hashes.
- Login throttling locks an email/IP pair for 10 minutes after five failed attempts.
- Administrator account provisioning through environment variables; public
  registration can only create customer accounts.
- SQLite order storage linked to customer user IDs.
- Four laundry services, calculated starting prices, and a ₱20/kg charge.
- Booking validation for weights up to 50 kg, pickup dates within 30 days, and
  hourly pickup slots during operating hours.
- Order tracking with forward-only status transitions and customer cancellation
  of pending orders.
- Actual post-pickup weighing recalculates the final price; the initial estimate
  is not treated as a final charge.
- Order status history records when each transition happened and who made it.
- Unpaid/paid tracking, simulated GCash/card reference recording, searchable and
  filterable admin orders, pickup sorting, daily metrics, and pagination.
- Customer profile details, password changes, and administrator-assisted
  customer password resets.
- Session-bound CSRF tokens, `HttpOnly` cookies, and `SameSite=Lax`.
- SQLite write-ahead logging and per-thread serialized JSON backup writes.
- Atomic JSON order backup with timestamped copies of the previous backup.
- Backups are synchronized between threads and retain only the 20 most recent
  timestamped copies.
- Pickup validation uses the Asia/Manila time zone.
- Responsive HTML/CSS templates and printable receipts.

## Windows setup

Use PowerShell from the project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Set an application secret and initial administrator credentials in the current
PowerShell session. Replace the example password with a unique password of at
least 12 characters:

```powershell
$env:FRESHNEST_SECRET_KEY = (python -c "import secrets; print(secrets.token_hex(32))")
$env:FRESHNEST_ADMIN_NAME = "FreshNest Admin"
$env:FRESHNEST_ADMIN_EMAIL = "admin@example.com"
$env:FRESHNEST_ADMIN_PASSWORD = "replace-with-a-unique-password"
python app.py
```

The configured administrator is created (or refreshed from these environment
values) at app startup. Updating `FRESHNEST_ADMIN_PASSWORD` will reset the admin
password at the next start. Do not use sample credentials in a deployed app. Keep
the secret and admin password out of source control. `.env.example` lists the
supported setting names; this app intentionally does not load `.env` files
automatically.

Open `http://127.0.0.1:5000/`. Create a customer account from the sign-in page.
The application runs with Flask debug mode disabled. For HTTPS deployments, set
`FRESHNEST_HTTPS_ONLY=true`.

If no secret is supplied during local development, the app generates a
temporary random secret and sessions will expire when it restarts. Set
`FRESHNEST_SECRET_KEY` to keep sessions stable between restarts.

## Project structure

```text
app.py                    Flask routes, authentication, CSRF, error handlers
db.py                     SQLite schema, migrations, and order/user queries
laundry_logic.py          Pricing, booking validation, and JSON backup helpers
templates/                Jinja pages, shared base, and error pages
static/css/style.css      Responsive styles and print stylesheet
static/js/script.js       Live price preview and date-specific pickup slots
static/img/laundry-hero.jpg  Local landing-page hero image
data/backup_orders.json   Current JSON order backup (created at runtime)
data/backups/             Timestamped previous JSON backups
test_laundry_logic.py     Logic tests for pricing, dates, and JSON files
test_app.py               Flask client tests for auth, CSRF, orders, and admin actions
```

## Database and data files

`freshnest.db` is created beside `app.py` and is excluded from Git. SQLite
contains:

- `users`: ID, full name, unique email, password hash, role, and creation time.
- `orders`: ID, nullable customer user ID, customer/contact/address snapshot,
  service, estimated and actual weight, payment method/reference/status and paid
  time, price, pickup date/time, order status, and creation time.
- `order_events`: status timeline with timestamp and the person who changed it.
- `login_attempts`: failed-login counters and lockout deadlines by email and IP.

Existing order tables are migrated by adding missing columns. Older orders that
were saved before account IDs existed remain available in the admin view, but
cannot be assigned to a customer automatically; their old names do not reliably
identify an account. Administrators can reset a customer password from the
customer-accounts section of the admin dashboard. Customers can update their
saved name/address/contact and change their password from their dashboard.

`data/backup_orders.json` mirrors order records only; it never contains the
`users` table or password hashes. It is not an account backup, is not encrypted,
and must be protected as sensitive customer data. Writes go to a temporary
file in the same directory and replace the current backup only after the full
JSON has been written. Before replacing an existing backup, FreshNest copies it
to `data/backups/` with a timestamp, retaining only the 20 newest copies. The
order backup preserves `user_id`; a restore links orders only when the matching
user record already exists in SQLite. If it does not, the order remains
unassigned and visible to administrators only. If the JSON is missing or
corrupted, startup logs a warning and keeps the SQLite database as the primary
store.

GCash and credit/debit card methods are simulated for this project; no payment
is processed. The app collects an administrator-entered transaction reference
only, and never asks for or stores card numbers. The final charge is calculated
from the actual weight entered by an administrator after pickup. Marking a
payment paid requires actual weight, requires a reference for simulated
electronic payments, and cannot be undone in the UI.

## Tests

Run the logic and Flask test-client suites using the active environment:

```powershell
python -m unittest -v test_app test_laundry_logic
```

The tests cover pricing and weight limits, pickup date/time rules, JSON
read/write behavior, registration and sign-in, lockout, CSRF, customer order
privacy, cancellation, status validation, actual weighing, payment references,
backup restoration, admin wildcard search, and static asset handling.

## Screenshots

Capture the running home page, customer dashboard, and admin order table at
desktop and mobile widths when preparing a project submission. Keep any local
database, credentials, or customer records out of screenshots.
