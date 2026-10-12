# FreshNest Laundry

FreshNest is a small Flask laundry booking app. Customers can create accounts,
schedule pickups, see their order history, cancel pending orders, and print
receipts. Administrators can manage order progress and record payments.

## Features

- Customer registration and password-based sign-in using Werkzeug password hashes.
- Administrator account provisioning through environment variables; public
  registration can only create customer accounts.
- SQLite order storage linked to customer user IDs.
- Four laundry services, calculated starting prices, and a ₱20/kg charge.
- Booking validation for weights up to 50 kg, pickup dates within 30 days, and
  hourly pickup slots during operating hours.
- Order tracking with forward-only status transitions and customer cancellation
  of pending orders.
- Unpaid/paid tracking, searchable and filterable admin orders, summary metrics,
  and pagination.
- Session-bound CSRF tokens, `HttpOnly` cookies, and `SameSite=Lax`.
- Atomic JSON order backup with timestamped copies of the previous backup.
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
values) at app startup. Do not use sample credentials in a deployed app. Keep
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
test_laundry_logic.py     Unit tests for pricing, dates, and JSON files
```

## Database and data files

`freshnest.db` is created beside `app.py` and is excluded from Git. SQLite
contains:

- `users`: ID, full name, unique email, password hash, role, and creation time.
- `orders`: ID, nullable customer user ID, customer/contact/address snapshot,
  service, weight, payment method/status, price, pickup date/time, order status,
  and creation time.

Existing order tables are migrated by adding missing columns. Older orders that
were saved before account IDs existed remain available in the admin view, but
cannot be assigned to a customer automatically; their old names do not reliably
identify an account.

`data/backup_orders.json` mirrors the SQLite orders. Writes go to a temporary
file in the same directory and replace the current backup only after the full
JSON has been written. Before replacing an existing backup, FreshNest copies it
to `data/backups/` with a timestamp. If the JSON is missing or corrupted,
startup logs a warning and keeps the SQLite database as the primary store.

## Tests

Run the dependency-free logic tests using the active environment:

```powershell
python -m unittest -v test_laundry_logic
```

The tests cover price calculations and weight limits, pickup date/time rules,
normal JSON reads/writes, timestamped backups, and corrupted JSON/UTF-8 files.

## Screenshots

Capture the running home page, customer dashboard, and admin order table at
desktop and mobile widths when preparing a project submission. Keep any local
database, credentials, or customer records out of screenshots.
