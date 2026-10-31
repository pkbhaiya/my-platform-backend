# My Platform — backend

Django REST Framework API for My Platform, a social media task platform:

- **Services and orders**: admins (and reseller panels over an API) place orders for a
  service; each order becomes one Task with a number of claim slots.
- **Tasks**: approved users claim a slot, open the target, submit the result; admins
  review submissions. Rejections add strikes; levels set strike limits.
- **Wallet**: approved work credits the user's wallet; users request UPI withdrawals that
  admins review. A daily job reconciles every wallet against its ledger.
- **Accounts**: sign-up with admin approval, levels, referrals with one-time bonuses,
  linked-account (shared IP/device) detection.
- **Admin tools**: analytics, platform settings with a change log, maintenance mode,
  Telegram alerts.
- **Reseller API**: an SMM API v2 endpoint compatible with Perfect Panel (`/api/v2`).

The React frontend is a separate project (`my-platform-frontend`).

## Technology

| | |
|---|---|
| Python | 3.13 (developed on 3.13.7) |
| Framework | Django 5.2, Django REST Framework 3.18 |
| Auth | JWT access/refresh tokens (djangorestframework-simplejwt) |
| Database | SQLite in development; MySQL 8.0.16+ in production (mysqlclient) |
| Cache | Local memory by default; Redis through `REDIS_URL` (django-redis) |
| Other | django-cors-headers, python-dotenv |

Direct dependencies are pinned in `requirements.in`; `requirements.txt` is generated
from it with hashes (see `PRODUCTION_SETUP.md`, section 2).

## Project structure

```
config/             settings, URLs, environment parsing (database, cache, security, dev LAN access)
accounts/           users, sign-up, login, levels, strikes, linked accounts, admin user management
common/             site settings, maintenance mode, Telegram alerts, analytics, public endpoints
services_catalog/   services that can be ordered
orders/             orders, admin order creation, reseller API (v2 and X-API-Key), admin wallets
tasks/              tasks, claims, submissions, reviews, expiry
wallet/             user wallets, ledger, withdrawals, reconciliation
referrals/          referral program, invite codes, bonuses
*/migrations/       database migrations (all committed)
*/management/       scheduled commands (see "Scheduled jobs")
PRODUCTION_SETUP.md deployment guide (MySQL, environment, cron, Perfect Panel)
```

## Setup

### 1. Virtual environment and dependencies

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r requirements.txt
```

`mysqlclient` compiles against the MySQL client library, even for SQLite development:

- macOS: `brew install mysql-client pkg-config`, then
  `export PKG_CONFIG_PATH="$(brew --prefix mysql-client)/lib/pkgconfig"`
- Debian/Ubuntu: `sudo apt install python3-dev default-libmysqlclient-dev build-essential pkg-config`

### 2. Environment variables

```bash
cp .env.example .env
```

`.env` is read automatically and must never be committed. For local development set
`DJANGO_DEBUG=true`: without it (or with any other value) Django starts with production
settings and refuses to run without production secrets. In development a random
`DJANGO_SECRET_KEY` is written to `.env` the first time Django starts.

Every variable is described in `.env.example`. Production needs at least
`DJANGO_SECRET_KEY`, `DJANGO_JWT_SIGNING_KEY`, `ACCOUNT_SIGNAL_SALT`,
`DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS` and the database settings
(see `PRODUCTION_SETUP.md`, section 3).

### 3. Database

- **Development**: SQLite at `db.sqlite3`, no configuration needed.
- **Production**: MySQL, through `DATABASE_URL`
  (`mysql://user:password@host:3306/name`) or `DB_ENGINE=mysql` with `DB_NAME`,
  `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` (`config/database.py`).

```bash
backend/.venv/bin/python manage.py migrate
backend/.venv/bin/python manage.py seed_default_levels   # optional: default user levels, keeps existing ones
backend/.venv/bin/python manage.py createsuperuser
```

### 4. Run

```bash
backend/.venv/bin/python manage.py runserver            # http://127.0.0.1:8000
backend/.venv/bin/python manage.py runserver 0.0.0.0:8000   # reachable from a phone on the same Wi-Fi (DEBUG only)
```

Health check: `GET /api/v1/health/` returns
`{"status": "ok", "service": "my-platform-backend", "time_utc": "..."}`.

The Django admin site is at `/admin/`.

## Authentication

- `POST /api/v1/auth/login/` returns a JWT `access` and `refresh` token; send
  `Authorization: Bearer <access>`; renew with `POST /api/v1/auth/refresh/`.
- `/api/v1/admin/...` requires a staff account.
- Accounts awaiting approval can use only their status, level and profile endpoints.
- `/api/v2` (reseller panels) takes the reseller key as the `key` form field;
  `/api/v1/orders/incoming/` and `/api/v1/services/incoming/` take it in the
  `X-API-Key` header.

## API overview

All routes below are taken from the URL configuration.

**Public**

| Method | Path |
|---|---|
| GET | `/api/v1/health/` |
| GET | `/api/v1/public/overview/`, `/api/v1/public/config/`, `/api/v1/approval-instructions/` |
| GET | `/api/v1/public/invite-code/<code>/` (rate-limited) |
| POST | `/api/v1/auth/signup/`, `/api/v1/auth/login/`, `/api/v1/auth/refresh/` |

**Signed-in user**

| Method | Path |
|---|---|
| GET, PATCH | `/api/v1/auth/me/` |
| GET | `/api/v1/auth/me/level/`, `/api/v1/auth/me/account-status/` |
| GET, POST | `/api/v1/auth/me/platform-usernames/` |
| GET | `/api/v1/tasks/available/`, `/api/v1/tasks/my/`, `/api/v1/tasks/history/`, `/api/v1/tasks/my/summary/`, `/api/v1/tasks/my/review-status/` |
| POST | `/api/v1/tasks/<task_id>/claim/`, `/api/v1/tasks/<task_id>/open-target/`, `/api/v1/tasks/<task_id>/submit/` |
| GET | `/api/v1/wallet/`, `/api/v1/wallet/transactions/` |
| GET, PUT | `/api/v1/wallet/payout-upi/` |
| GET, POST | `/api/v1/wallet/withdrawals/` |
| GET | `/api/v1/referrals/me/`, `/api/v1/referrals/me/list/` |

**Admin (staff)**

| Area | Paths |
|---|---|
| Users | `/api/v1/admin/users/` (GET), `/api/v1/admin/users/<id>/` (GET), `.../status/` (PATCH), `.../profile/` (PATCH), `.../set-password/` (POST), `.../activity/` (GET), `.../tasks/` (GET); `/api/v1/auth/users/` (GET), `/api/v1/auth/users/<id>/status/` (PATCH) |
| Levels, strikes, duplicates | `/api/v1/admin/user-levels/` (GET, POST), `/api/v1/admin/user-levels/<id>/` (PATCH), `/api/v1/admin/task-strikes/` (GET), `/api/v1/admin/duplicate-registrations/` (GET) |
| Services | `/api/v1/admin/services/` (GET, POST), `/api/v1/admin/services/<id>/` (GET, PATCH), `/api/v1/admin/services/bulk-delete/` (POST) |
| Orders | `/api/v1/admin/orders/` (GET), `/api/v1/admin/orders/create/` (POST), `/api/v1/admin/orders/<id>/previous-orders/` (GET), `/api/v1/admin/orders/bulk-delete/` (POST) |
| Tasks and reviews | `/api/v1/admin/tasks/pending/`, `/api/v1/admin/tasks/history/`, `/api/v1/admin/tasks/review-history/`, `/api/v1/admin/tasks/<id>/submissions/` (GET); `/api/v1/admin/tasks/<id>/review/`, `.../resume/`, `.../cancel/`, `/api/v1/admin/task-submissions/<claim_id>/review/`, `/api/v1/admin/tasks/bulk-delete/` (POST) |
| Wallets and withdrawals | `/api/v1/admin/wallet/transactions/`, `/api/v1/admin/wallet/withdrawals/` (GET), `/api/v1/admin/wallet/withdrawals/<id>/review/` (POST), `/api/v1/admin/wallet/reconciliation/` (GET), `/api/v1/admin/wallet/reconciliation/<run_id>/review/` (POST) |
| Admin wallets (reseller funds) | `/api/v1/admin/my-wallet/` (GET, PATCH), `.../add-funds/`, `.../regenerate-key/` (POST), `.../transactions/` (GET); `/api/v1/admin/admin-wallets/` (GET), `/api/v1/admin/admin-wallets/<id>/` (GET), `.../transactions/` (GET), `.../adjustments/` (POST) |
| Referrals | `/api/v1/admin/referrals/`, `/api/v1/admin/referrals/top/` (GET), `/api/v1/admin/referrals/<id>/approve/`, `.../reject/` (POST) |
| Settings and maintenance | `/api/v1/admin/platform-settings/` (GET, PATCH), `.../changes/` (GET), `.../test-alert/` (POST), `/api/v1/admin/maintenance/` (GET) |
| Analytics and dashboard | `/api/v1/admin/analytics/`, `.../orders/`, `.../orders/<id>/`, `.../users/`, `.../payouts/`, `/api/v1/admin/dashboard/stats/` (GET) |

**Reseller API**

| Method | Path |
|---|---|
| POST | `/api/v2` (actions `services`, `add`, `status`, `balance`; see `PRODUCTION_SETUP.md`, section 12) |
| POST | `/api/v1/orders/incoming/` |
| GET | `/api/v1/services/incoming/` |

## Scheduled jobs

| Command | When |
|---|---|
| `manage.py expire_claims` | every minute (expires stale claims and old orders) |
| `manage.py process_referrals` | daily |
| `manage.py reconcile_wallets` | daily (reports mismatches; never changes balances) |
| `manage.py purge_account_signals` | daily (data retention) |

Cron lines are in `PRODUCTION_SETUP.md`, section 8.

## Tests

```bash
backend/.venv/bin/python manage.py test
backend/.venv/bin/python manage.py check
backend/.venv/bin/python manage.py makemigrations --check --dry-run
```

- `accounts/test_states.py` compares the state list with the frontend's
  `src/constants/states.js`. It looks for the frontend next to this folder
  (`../my-platform-frontend`); set `FRONTEND_DIR` otherwise. Without the frontend that
  one test is skipped.
- `orders.test_admin_wallets.ParallelOrderTests` runs orders in parallel threads and can
  fail intermittently on SQLite with "database table is locked".

## Deployment

See `PRODUCTION_SETUP.md` for MySQL, environment variables, migrations, static files,
cron jobs, HTTPS and connecting Perfect Panel.
