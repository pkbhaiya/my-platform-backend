# TaskZippy production setup (MySQL)

How to bring up a new production backend on an empty MySQL database. Only the
configuration is carried over from the development database: Services, User
Levels and Site Configuration. Demo orders, tasks, claims, users, wallets and
ledger rows stay behind.

Paths below assume the code is deployed to `/srv/my-platform` with a virtualenv
at `/srv/my-platform/.venv`. Adjust them to your server.

## 1. MySQL

Use **MySQL 8.0.16 or newer** (8.4 LTS recommended). The schema relies on CHECK
constraints, which MySQL enforces only from 8.0.16. No time zone tables are
needed: Django stores UTC and computes local (`Asia/Kolkata`) day boundaries
itself.

```sql
CREATE DATABASE my_platform CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'my_platform'@'%' IDENTIFIED BY 'a-long-random-password';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES
  ON my_platform.* TO 'my_platform'@'%';
FLUSH PRIVILEGES;
```

Replace `'%'` with the application server's address if MySQL is reachable from
other hosts.

## 2. Code and dependencies

```bash
cd /srv/my-platform
python3 -m venv .venv
# mysqlclient compiles against the MySQL client library, so the server needs
# pkg-config and the MySQL client headers first:
#   Debian/Ubuntu: sudo apt install python3-dev default-libmysqlclient-dev build-essential pkg-config
#   macOS:         brew install mysql-client pkg-config
#                  export PKG_CONFIG_PATH="$(brew --prefix mysql-client)/lib/pkgconfig"
.venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/pip install gunicorn    # or the WSGI server you prefer
```

`requirements.txt` pins every package, including indirect ones, with its
SHA-256 hashes; `--require-hashes` refuses any file that does not match, so a
replaced or tampered package on the index is never installed.

To change a version, edit `requirements.in` (the direct dependencies) and
regenerate the lock with pip-tools in a development virtualenv, then commit both files:

```bash
backend/.venv/bin/pip install pip-tools==7.6.2
# The placeholder flags let pip-tools read mysqlclient's metadata on a machine
# without the MySQL client libraries; nothing is compiled.
MYSQLCLIENT_CFLAGS=-I/usr/include MYSQLCLIENT_LDFLAGS=-lmysqlclient \
  backend/.venv/bin/pip-compile --generate-hashes --strip-extras requirements.in
```

Compile with the same Python minor version as the server (the header of
`requirements.txt` names it).

## 3. Environment

Put these in `/srv/my-platform/.env` (read automatically by `config/settings.py`)
or in the service manager's environment. `chmod 600 .env`.

| Variable | Production value |
|---|---|
| `DJANGO_DEBUG` | `false` (or unset: only the exact value `true` turns DEBUG on) |
| `DJANGO_SECRET_KEY` | **Required.** 50+ random characters: `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `DJANGO_JWT_SIGNING_KEY` | **Required.** Signs login tokens. 50+ random characters, generated the same way and different from the secret key. Changing it signs everyone out. |
| `DJANGO_ALLOWED_HOSTS` | `api.example.com` (comma-separated) |
| `CORS_ALLOWED_ORIGINS` | `https://app.example.com` (the frontend origin) |
| `CSRF_TRUSTED_ORIGINS` | `https://api.example.com` (needed for the Django admin over HTTPS) |
| `DATABASE_URL` | `mysql://my_platform:URL-ENCODED-PASSWORD@db-host:3306/my_platform` |
| `DB_CONN_MAX_AGE` | `60` |
| `DJANGO_SECURE_SSL_REDIRECT` | `true` (leave `true` unless the proxy already redirects) |
| `DJANGO_BEHIND_TLS_PROXY` | `true` only if a trusted proxy terminates TLS and sets `X-Forwarded-Proto` |
| `DJANGO_NUM_PROXIES` | `1` behind one reverse proxy that sets `X-Forwarded-For`; `0` if clients connect directly |
| `DJANGO_SECURE_HSTS_SECONDS` | unset means `3600` (one hour). Raise to `31536000` (one year) once HTTPS works on every host: browsers then refuse plain HTTP for that long, so make sure first. |
| `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` | `true` only if every subdomain is HTTPS |
| `DJANGO_STATIC_ROOT` | `/srv/my-platform/staticfiles` (optional; this is the default) |
| `PUBLIC_SERVICE_RATES` | `true` or `false` (show rates on the public landing page) |
| `PROVIDER_API_RATE` | `120/minute` (SMM API v2 requests per client IP; see section 12) |
| `PUBLIC_API_RATE` | `60/minute` (per client IP, for each public endpoint: landing overview, public config, invite-code check) |
| `REDIS_URL` | `redis://127.0.0.1:6379/1`: one cache for every worker, so rate limits are exact (section 9). Unset: per-process memory, and `check --deploy` warns. |
| `PROVIDER_API_REQUIRE_HTTPS` | leave unset: `/api/v2` refuses plain HTTP whenever `DJANGO_DEBUG=false` |
| `PROVIDER_API_LOG_LEVEL` | `INFO` (one line per `/api/v2` request, never the key) or `WARNING` |
| `ACCOUNT_SIGNAL_SALT` | **Required** when `DJANGO_DEBUG=false`: 32+ random characters, different from the secret key. Sign-up/login IP addresses and device IDs are stored only as HMAC hashes under it. Never change it later, or linked-account and referral checks stop matching older records. |
| `AUTH_COOKIE_SECURE` | leave unset (defaults to `true` when `DJANGO_DEBUG=false`). Marks the `device_id` cookie Secure. |
| `TELEGRAM_ALERTS_ENABLED` | `true` to send admin alerts (section 13); `false` or unset sends nothing |
| `TELEGRAM_BOT_TOKEN` | the bot token from @BotFather. Secret: never put it in the frontend or logs. |
| `TELEGRAM_ADMIN_CHAT_ID` | the admin chat or group ID, e.g. `-1001234567890` |

Instead of `DATABASE_URL` you may set `DB_ENGINE=mysql`, `DB_NAME`, `DB_USER`,
`DB_PASSWORD`, `DB_HOST` and `DB_PORT`. Every MySQL connection uses utf8mb4,
`STRICT_TRANS_TABLES` and READ COMMITTED (`config/database.py`).

There is no API key variable. Each administrator gets one personal API key and
one admin wallet in the admin panel (step 7).

Django refuses to start without `DJANGO_SECRET_KEY`, `DJANGO_JWT_SIGNING_KEY`
and `ACCOUNT_SIGNAL_SALT` unless `DJANGO_DEBUG=true`, so a forgotten variable
never falls back to development settings. With `DJANGO_DEBUG=true` it also refuses
to start if `DJANGO_ALLOWED_HOSTS` names anything but localhost or a private LAN
address: debug pages must never be served under a public name. Development
needs only `DJANGO_DEBUG=true` in `.env`; a random development secret key is
written there on first start (`config/security.py`).

## 4. Create the schema

```bash
cd /srv/my-platform
.venv/bin/python manage.py migrate
.venv/bin/python manage.py check --deploy
.venv/bin/python manage.py collectstatic --noinput
```

The data migrations print nothing on an empty database. `check --deploy` should
report only two HSTS notes: `security.W005` until you set
`DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=true` (only when every subdomain is
HTTPS), and `security.W021`, because HSTS preloading is not enabled (a separate,
long-term commitment). A `common.W001` warning means `REDIS_URL` is missing.

## 5. Copy the configuration from development

On the development machine, against the migrated development database:

```bash
cd /path/to/my-platform        # this backend's folder
backend/.venv/bin/python manage.py dumpdata \
  services_catalog.Service accounts.UserLevel common.SiteConfiguration \
  --indent 2 --output seed_config.json
```

Review `seed_config.json` before using it:

- **Services** keep their IDs, so resellers' service IDs stay valid. Check each
  service's `is_active` value. All three development services are currently inactive.
- **User Levels** keep their codes, which services reference.
- **Site Configuration** carries contact details, the minimum withdrawal, the
  target wait, the claim timeout and the auto-pause thresholds.
- The file contains no secret. Admin wallet keys (hashes) and balances live in
  separate tables that are not exported.

Copy it to the server, load it, then delete it:

```bash
cd /srv/my-platform
.venv/bin/python manage.py loaddata seed_config.json
rm seed_config.json
```

MySQL continues each auto-increment counter after the highest loaded ID, so new
services get fresh IDs. This round trip is covered by
`common/test_seed_config.py`.

**Do not export or load anything else.** Leave behind:
- `orders.Order`, `orders.AdminWallet` and `orders.AdminWalletTransaction`
- `tasks.Task`, `tasks.TaskClaim`, `tasks.TaskUserClaim` and `tasks.TaskStrike`
- `wallet.Wallet`, `wallet.WalletTransaction` and `wallet.WithdrawalRequest`
- `accounts.User`, `accounts.DuplicateRegistrationFlag` and `accounts.PlatformUsername`
- Sessions

These are demo records, and the users and wallets would bring personal data and balances.

## 6. Create the administrator

```bash
.venv/bin/python manage.py createsuperuser
```

It asks for email, full name, platform username, WhatsApp number, state, gender
(`male`, `female`, `other` or `not_disclosed`) and a password. Superusers are created
approved.

## 7. Admin wallets and API keys (one per administrator)

Every staff account has exactly one admin wallet, created automatically (for
existing staff by the migration, for new staff as soon as the account is saved
as staff). Each administrator signs in, opens **Admin → My Wallet** and presses
**Generate key**, then copies the key into their reseller panel. The key is shown
once; only its SHA-256 hash and first 8 characters are stored.

The wallet is a prepaid INR balance, starting at ₹0.00. Every order placed with
the key (/api/v2 or the X-API-Key endpoints) is charged to it at creation
(rate × quantity / 1,000, rounded half up to the paisa); with too little balance
the order is refused with `Not enough funds on balance`. Partial orders refund
the undelivered part (rounded down) and cancelled orders the whole charge,
automatically and once. Manual orders (Create Order) are never charged. Use
**Add funds** (UPI reference required) on My Wallet. **Regenerate key** stops
the old key immediately. A key is refused while its owner is inactive or not
staff. Superusers see every wallet under **Admin → Admin Wallets** (read-only)
and can record an **Adjustment** (note required, never below ₹0.00, written to
that admin's audit log).

A database upgraded from the API-client version (orders.0014) gives the main
superuser's wallet the key of the client **Default** (the single Phase 6 key),
so that Perfect Panel connection keeps working. Other clients' balances and
ledgers move to the wallet of the admin who created them; their key moves too
when that wallet has none yet, otherwise it stops working (the migration prints
which). Add funds before the panel's next order.

## 8. Expire stale claims and old orders every minute

Requests already expire stale claims as they arrive. The cron job also covers
quiet periods, so abandoned claims release their slots on time. The same job
cancels Orders still open after **Platform Settings → Order expiry (hours)**
(default 72): reason `expired`, approved work and rewards kept, the Order ends
Partial or Canceled.

```cron
* * * * * cd /srv/my-platform && /usr/bin/flock -n /tmp/my-platform-expire-claims.lock /srv/my-platform/.venv/bin/python manage.py expire_claims >> /var/log/my-platform/expire_claims.log 2>&1
```

`flock -n` skips a run while the previous one is still going. Overlapping runs
would also be safe, because each claim expires through a conditional update.
Create `/var/log/my-platform/` first.

If this job stops for 10 minutes, the admins get a Telegram alert (section 13).

### Daily jobs at 03:00 IST

| Command | What it does |
|---|---|
| `process_referrals` | Pays qualified referral bonuses (honouring the monthly cap), expires referrals past their deadline, re-checks abuse, and sends the daily referral summary alert. |
| `reconcile_wallets` | Checks every wallet balance against its ledger, and every admin wallet's balance against its funds added, charges, refunds and adjustments (one charge per API order, the right refund for each partial or cancelled one). It **never corrects a balance**: mismatches are listed in **Admin → Reconciliation** and alerted. |
| `purge_account_signals` | Deletes login-IP signals not seen for 180 days (data retention). |
| `flushexpiredtokens` | Deletes expired refresh-token records (SimpleJWT token blacklist): every sign-in records one so a password change can revoke it. |

03:00 IST is 21:30 UTC on the previous day. Most servers run cron in UTC:

```cron
30 21 * * * cd /srv/my-platform && /usr/bin/flock -n /tmp/my-platform-daily.lock sh -c '.venv/bin/python manage.py process_referrals; .venv/bin/python manage.py reconcile_wallets; .venv/bin/python manage.py purge_account_signals; .venv/bin/python manage.py flushexpiredtokens' >> /var/log/my-platform/daily.log 2>&1
```

Tokens carry a hash of the user's password hash (`SIMPLE_JWT["CHECK_REVOKE_TOKEN"]`),
so an admin's **Change password** signs that user out of every device at once.
Tokens issued before this setting existed have no such hash: after deploying it,
everyone signs in once more.

With cronie (RHEL, Fedora, Arch), you can write the time in IST instead.
Debian's default cron ignores `CRON_TZ`, so there keep the UTC line above.

```cron
CRON_TZ=Asia/Kolkata
0 3 * * * cd /srv/my-platform && /usr/bin/flock -n /tmp/my-platform-daily.lock sh -c '.venv/bin/python manage.py process_referrals; .venv/bin/python manage.py reconcile_wallets; .venv/bin/python manage.py purge_account_signals; .venv/bin/python manage.py flushexpiredtokens' >> /var/log/my-platform/daily.log 2>&1
```

The commands are separated by `;`, so one failing does not skip the others.
`process_referrals` runs first, so reconciliation also checks the bonuses it
just paid. All three jobs keep running during maintenance mode.

## 9. Run the application

```bash
.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 3 \
  --timeout 30 --limit-request-line 4094 --limit-request-fields 50
```

`--timeout 30` restarts a worker stuck on one request for 30 seconds, and the
two limits refuse an oversized request line or too many header lines before
Django sees them, so one slow or oversized request cannot hold a worker.

Put nginx, or another reverse proxy, in front: TLS, `/static/` served from
`DJANGO_STATIC_ROOT`, request limits, and `X-Forwarded-For` / `X-Forwarded-Proto`
headers (see *Reverse-proxy limits* below). Match `DJANGO_NUM_PROXIES` and
`DJANGO_BEHIND_TLS_PROXY` to that setup.

Build the frontend against this API:

```bash
cd /path/to/my-platform-frontend
VITE_API_BASE_URL=https://api.example.com npm run build   # serve dist/
```

`VITE_API_BASE_URL` is required for the production build; without it the app
calls the API on its own origin. The development-only LAN access (this
computer's private address in `ALLOWED_HOSTS`, private-network origins for
CORS; see `config/dev_network.py`) is switched off whenever `DJANGO_DEBUG=false`.

### Redis (shared rate limits)

Rate limits (login, sign-up, token refresh, the public endpoints and `/api/v2`)
and the landing-page overview live in Django's cache. Set `REDIS_URL` so every
gunicorn worker shares one Redis cache. Without it each worker keeps its own
in-memory counts, so three workers allow three times each limit, and
`check --deploy` warns (`common.W001`).

```bash
sudo apt install redis-server     # Debian/Ubuntu; listens on 127.0.0.1 only by default
redis-cli ping                    # PONG
```

Then `REDIS_URL=redis://127.0.0.1:6379/1` (database 1 for this app). Use
`rediss://` for TLS, or `unix:///run/redis/redis.sock?db=1` for a socket. Keep
Redis on localhost or the private network, never the internet; on a shared host
set `requirepass` and use `redis://:PASSWORD@host:6379/1`. It holds only
counters and short-lived cached responses, so it needs no backup. If Redis is
unreachable, API requests fail (after at most 2 seconds) until it is back, so
let systemd restart it and include it in monitoring.

The public endpoints (`/api/v1/public/overview/`, `/api/v1/public/config/` and
the invite-code check) allow `PUBLIC_API_RATE` requests per client IP, counted
separately for each endpoint (default `60/minute`). The invite-code check also
keeps its own 20/minute limit against code guessing.

### Reverse-proxy limits

Django parses a request body before it can check who sent it, so cap request
sizes and slow clients at the proxy. A starting point for nginx (adjust the
names and paths; raise `client_max_body_size` only if an endpoint ever needs uploads):

```nginx
# In the http { } block:
limit_req_zone $binary_remote_addr zone=api:10m    rate=10r/s;
limit_req_zone $binary_remote_addr zone=api_v2:10m rate=2r/s;   # /api/v2: 120/minute
limit_req_status 429;

server {
    listen 443 ssl;
    server_name api.example.com;
    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;

    client_max_body_size        1m;
    client_header_buffer_size   1k;
    large_client_header_buffers 4 8k;
    client_body_timeout         10s;
    client_header_timeout       10s;
    keepalive_timeout           15s;

    proxy_read_timeout 30s;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;

    location /api/v2 {
        limit_req zone=api_v2 burst=20 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }

    location /api/ {
        limit_req zone=api burst=40 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }

    location /admin/ {
        limit_req zone=api burst=40 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }

    location /static/ {
        alias /srv/my-platform/staticfiles/;
    }
}
```

The `proxy_*` lines at server level apply to every `location` that has no
`proxy_set_header` of its own. `X-Forwarded-For $remote_addr` replaces whatever
the client sent, which is what `DJANGO_NUM_PROXIES=1` expects;
`X-Forwarded-Proto $scheme` goes with `DJANGO_BEHIND_TLS_PROXY=true`. Django's
own rate limits still apply behind these: nginx only stops floods and oversized
or slow requests before they reach a worker. `/api/v2` is the strictest because
one reseller panel sends from one address; its 2 per second matches
`PROVIDER_API_RATE`'s default of 120/minute, so raise both together.

### Serving the TaskZippy app (installable)

The user panel can be installed on phones (a PWA). For that the frontend must
be served over **HTTPS**, and the browser must always see a fresh service
worker so that **New version available — Refresh** appears after a deploy:

```nginx
location = /sw.js                { add_header Cache-Control "no-cache"; }
location = /manifest.webmanifest { add_header Cache-Control "no-cache"; }
location = /index.html           { add_header Cache-Control "no-cache"; }
location /assets/                { add_header Cache-Control "public, max-age=31536000, immutable"; }
location /                       { try_files $uri /index.html; }   # client-side routes
```

The service worker caches only the app shell and static files. It never
caches or answers `/api/*` (tasks, wallet and balances are always live) and
leaves `/admin` alone; the admin panel is not part of the installable app.

**Installing needs HTTPS.** Users install from **Install App** at the bottom
of the user panel's menu. Over HTTPS, Chrome on Android shows **Install now**
(its own install dialog); iPhone Safari shows the Share → Add to Home Screen
steps. On plain HTTP (for example a phone opening the development server at
`http://192.168.x.x:5173`) the browser offers no install prompt and no service
worker runs, so only the manual steps appear (⋮ menu → Add to Home screen),
and the result is a plain shortcut rather than the installed app.
For link previews (WhatsApp, Facebook), change `og:image` in `index.html` to
the full address, e.g. `https://app.example.com/og-image.png`, before building.

## 10. DEBUG=False checklist

- [ ] `DJANGO_DEBUG=false`, a fresh `DJANGO_SECRET_KEY` (never a development one) and its own `DJANGO_JWT_SIGNING_KEY`.
- [ ] `DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` list only the real domains.
- [ ] HTTPS everywhere; `check --deploy` shows nothing beyond the HSTS notes of section 4. HSTS starts at one hour; raise `DJANGO_SECURE_HSTS_SECONDS` to `31536000` once every host is confirmed on HTTPS.
- [ ] `DJANGO_NUM_PROXIES` matches the proxy chain. Otherwise rate limits use the proxy's address, or can be dodged.
- [ ] `REDIS_URL` is set and `redis-cli ping` answers, so rate limits are shared by every worker (`check --deploy` shows no `common.W001`).
- [ ] The nginx request limits and the gunicorn `--timeout` / `--limit-request-*` flags from section 9 are in place.
- [ ] `collectstatic` has run, and `/static/` is served by the web server.
- [ ] `migrate` has run; `showmigrations` shows no `[ ]`.
- [ ] Only Services, User Levels and Site Configuration were loaded; no demo data.
- [ ] Each service's `is_active`, prices, reward % and fulfillment-cost settings are reviewed.
- [ ] An administrator exists, and each admin whose reseller panel places orders has generated a key and funded their wallet (Admin → My Wallet).
- [ ] The `expire_claims` cron job is installed and logging (it expires claims and old orders).
- [ ] The daily 03:00 IST job (`process_referrals`, `reconcile_wallets`, `purge_account_signals`) is installed and logging.
- [ ] `ACCOUNT_SIGNAL_SALT` is set to its own random value and recorded somewhere safe (it must never change).
- [ ] Telegram alerts are configured and a test alert arrived (section 13), or deliberately left off.
- [ ] The provider API answers over HTTPS only (section 12), and `DJANGO_BEHIND_TLS_PROXY` / `DJANGO_NUM_PROXIES` match the proxy.
- [ ] The MySQL user has privileges only on `my_platform`; the database is not public.
- [ ] Daily backups, for example `mysqldump --single-transaction --routines my_platform | gzip > backup-$(date +%F).sql.gz`, stored off the server and restore-tested.
- [ ] `.env` is `chmod 600` and is not in version control.

## 11. Smoke test after deployment

1. `GET https://api.example.com/api/v1/health/` returns `{"status": "ok", ...}`.
2. Admin sign-in works, and Analytics, Services and Task History load.
3. `GET /api/v1/services/incoming/` with the new `X-API-Key` lists only active API services.
4. A test order for a small quantity creates one Task; a worker can claim it, and the claim shows its time limit.
5. `POST https://api.example.com/api/v2` with a client's `key` and `action=balance` returns that client's balance, `{"balance": "...", "currency": "INR"}` (section 12).

## 12. Connecting Perfect Panel

The platform speaks the standard SMM API v2, so a Perfect Panel can add it as a
provider. Orders placed through it use the same rules as every other order:
service checks, the live capacity limit, one Task per Order.

### What to enter in Perfect Panel

In Perfect Panel, open the provider settings and add a new provider:

| Field | Value |
|---|---|
| API URL | `https://api.example.com/api/v2` (this backend's domain, not the frontend's) |
| API key | The admin's personal key from **Admin → My Wallet** (step 7). It is shown once. |

The URL must be **https**. In production `/api/v2` answers plain HTTP with
`{"error": "HTTPS is required"}`; the HTTPS redirect cannot help because a
redirected POST loses its body. Behind a reverse proxy that terminates TLS,
set `DJANGO_BEHIND_TLS_PROXY=true` (this enables `SECURE_PROXY_SSL_HEADER`, so
Django trusts `X-Forwarded-Proto: https`). The proxy must always set that header
and must not pass a client-supplied one through.

Regenerating the key on My Wallet stops the old key at once; paste the new one
into that Perfect Panel straight away.

### Verify the connection

1. **Balance.** Perfect Panel shows the provider balance: the key owner's
   wallet balance in **Admin → My Wallet**. Add funds first; at ₹0.00 every
   order is refused with `Not enough funds on balance`.
2. **Services sync.** Import the provider's services. Each active service with
   order source *API* or *API and manual* appears with its category, rate
   (INR per 1,000), minimum and maximum. Set the provider currency to INR in
   Perfect Panel, or set its exchange rate.
3. **Test order.** Map a Perfect Panel service to one of these, then place an
   order of the **minimum quantity** with a real link. In **Admin → Orders** a
   new API order appears with one Task.
4. **Watch the status.** Perfect Panel polls `status`. It moves from
   **Pending** (no claims yet), to **In progress** (workers have claimed),
   to **Completed** (every unit approved). It ends **Partial** or
   **Canceled** when an admin cancels the Task, or when the order expires
   after the configured order expiry.

### Behaviour to know

- **Charge** = rate × quantity / 1,000, from the server's rate only, rounded
  **ROUND_HALF_UP to 2 decimal places** (rate 200, quantity 500 → `100.00`;
  rate 0.5, quantity 10 → `0.01`). That amount is debited from the client's
  balance when the order is created, and `status` reports exactly it. An order
  that would cost less than ₹0.01 is refused. The stored order value keeps all
  8 decimal places.
- **Partial and Canceled** orders report the full original charge with
  `remains`; Perfect Panel refunds its customer from `remains`, and this
  platform refunds the client's balance the same way: charge × remains /
  quantity, rounded down (Partial), or the whole charge (Canceled).
- **Each client sees only its own orders** in `status`; any other ID is
  `Incorrect order ID`.
- **Not supported:** drip-feed (`runs`/`interval` are refused), refill and
  cancel (`refill`, `cancel` and `dripfeed` are `false` in `services`).
- **Retries:** `add` carries no external ID, so each request creates a new
  order. If Perfect Panel reports an error for an `add`, check **Admin →
  Orders** before resending.
- **Capacity:** `max` is the stable **Maximum order quantity** set on the
  service. An order is still refused if it exceeds today's live limit
  (eligible users × max order size %). The Service form shows that live limit.
- **Rate limit:** `PROVIDER_API_RATE` per client IP (default 120/minute).
  `DJANGO_NUM_PROXIES` must match the proxy chain, or every request counts
  against the proxy's address. Every reply is HTTP 200 JSON; refusals,
  including rate limiting, are `{"error": "..."}`.
- **Logs:** one line per request (action, IP, outcome, timing, service,
  quantity, order). The key is never logged and is masked in error reports.
- The older `/api/v1/services/incoming/` and `/api/v1/orders/incoming/`
  endpoints still work with a client's key in the `X-API-Key` header; their
  orders are charged the same way, and `external_order_id` is unique per client.

### Requests (for testing with curl)

```bash
API=https://api.example.com/api/v2
KEY=paste-the-key-here

curl -s -X POST "$API" -d key="$KEY" -d action=services
curl -s -X POST "$API" -d key="$KEY" -d action=add -d service=5 -d link="https://instagram.com/p/abc" -d quantity=100
curl -s -X POST "$API" -d key="$KEY" -d action=status -d order=42
curl -s -X POST "$API" -d key="$KEY" -d action=status -d orders=42,43,44
curl -s -X POST "$API" -d key="$KEY" -d action=balance
# JSON bodies work too:
curl -s -X POST "$API" -H 'Content-Type: application/json' -d "{\"key\":\"$KEY\",\"action\":\"balance\"}"
```

## 13. Telegram alerts

Admins can get short alerts in a Telegram chat: new orders (batched when busy),
auto-paused Tasks, withdrawal requests, reconciliation mismatches, expired
orders, a stopped `expire_claims` job, flagged referrals, the daily referral
summary, maintenance mode changes, an admin wallet's balance falling below its
threshold (once per crossing), funds added to a wallet, and orders refused for
insufficient funds (at most once per wallet every 15 minutes). Each can be switched off in **Platform
Settings → Telegram alerts**. Messages start with `[TaskZippy]` and carry IDs,
counts and amounts only: never a UPI ID, email, name or key.

1. Create a bot with @BotFather and copy its token into `TELEGRAM_BOT_TOKEN`.
2. Add the bot to the admin group, send any message there, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` into
   `TELEGRAM_ADMIN_CHAT_ID`. A group ID starts with `-100`.
3. Set `TELEGRAM_ALERTS_ENABLED=true` and restart the application.
4. In **Admin → Platform Settings → Notifications**, choose **Send test message**.
   It is sent at once whatever the switches say, and the page explains what
   is missing if it fails.

Alerts are sent after the database change commits, with a 5-second timeout.
A Telegram failure is logged and never blocks the order, review or withdrawal
that triggered it.

## 14. Maintenance mode

Use it during deployments, database work or incidents: **Admin → Platform
Settings → Maintenance**, switch **Maintenance mode** on and save. While it is on:

- Every user-panel API request gets HTTP 503
  `{"maintenance": true, "message": "...", "expected_end": "..."}`, and the
  user panel shows a full-page maintenance screen with the message, the
  expected end ("Expected back at 4:30 PM", India time) and the WhatsApp
  contact. Sign-in, the current-user request, health checks and the public
  settings stay open.
- Staff accounts bypass it completely, so the admin panel keeps working. A
  yellow "Maintenance mode is ON" banner with a **Turn off** button shows on
  every admin page.
- Sign-up is refused, whatever the registration settings say.
- `/api/v2` keeps accepting orders (their Tasks wait) unless **Keep accepting
  /api/v2 orders during maintenance** is unticked. Then `add` answers
  `{"error": "Service temporarily unavailable"}`; `services`, `status` and
  `balance` always work.
- Cron jobs keep running. Claim deadlines are paused: a claim that still had
  time when maintenance began is never expired during it. When maintenance
  ends, each such claim's deadline moves back by the time maintenance lasted.
  A claim that was already overdue when maintenance began expires as usual.

Every switch is logged (who and when, with the duration and the number of
claims extended), shown under **Recent maintenance windows**, and sent as a
Telegram alert.

## 15. Platform settings

All settings are edited in **Admin → Platform Settings**, one tab per area
(General, Registration & Access, Maintenance, Tasks, Wallet & Withdrawals,
Referrals, Notifications). **General** also holds the Telegram link
(`https://t.me/…` or `https://telegram.me/…` only) and the switch for the round
Telegram button shown on the user panel and the sign-in and sign-up pages;
with no link or the switch off, no button is shown. Each tab saves only its own changed fields;
the backend validates every value and range. Settings are read from the
database on every request (nothing is cached), so a saved change applies at
once without a restart. Each change is recorded with the admin, the time and
the old and new value; open **Recent changes** at the bottom of a tab. The
Django admin shows the settings read-only, so no change bypasses that check
and record.

Two withdrawal limits are off (0) until you set them:

- **Maximum per day**: the most one user can request per day (India time),
  pending and approved requests together; rejected requests do not count.
- **First-withdrawal hold**: a user's first withdrawal can be approved only
  this many hours after it was requested. It can be rejected at any time.
The expected end is cleared when maintenance is turned off.
