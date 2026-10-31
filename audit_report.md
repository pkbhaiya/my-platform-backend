# Platform Audit Report: Services, Orders, Tasks, Rewards, Wallet

**Date:** 2026-10-24  
**Type:** Read-only audit  
**Backend:** `my-platform` (this repository; Django 5.2.6, DRF 3.16.1 at the time)  
**Frontend:** `my-platform-frontend` (separate repository; React 19 + Vite)

## How this audit was done

- **Code.** Every statement comes from reading the code. Line numbers refer to the files as they are today. The backend includes the code-review changes made earlier in this session.
- **Pending migration.** Migration `tasks/0010_portable_task_per_order_constraint` exists in code but is **not applied** to `db.sqlite3`. The latest applied `tasks` migration is `0009_target_wait_seconds`.
- **Database.** Queries opened the real `db.sqlite3` as `file:…?mode=ro` with `PRAGMA query_only=ON` (verified to return `1`). The file's modification time (`1792830590`) and size (`430080` bytes) were identical before and after all queries.
- **Section 7 reproduction.** This ran DRF `is_valid()` and model `full_clean()` in memory on that read-only connection. Nothing was saved.
- **Terminology.**
  - There is no separate **Submission** or **Review** model: **NOT FOUND**. A single `TaskClaim` row holds the claim, the submission and the review outcome.
  - The ledger is `WalletTransaction`, and payouts are `WithdrawalRequest`. No other ledger or payout model exists.
- **Signals.** None exist. A search for `post_save`, `pre_save` and `@receiver` found nothing (**NOT FOUND**).
- **Defaults.** `DEFAULT_AUTO_FIELD = BigAutoField`. Django adds a database index for every `ForeignKey` automatically.

---

## 1. Data models

### 1.1 `Service` — `services_catalog/models.py:8-173`

| Field | Type | null / blank / default | Validators |
|---|---|---|---|
| `service_id` | `BigAutoField(primary_key=True)` (L24-26) | — | — |
| `name` | `CharField(max_length=150)` (L28) | required | — |
| `description` | `TextField` (L30) | `blank=True` | — |
| `price_per_1000` | `DecimalField(max_digits=14, decimal_places=4)` (L33-40) | required | `MinValueValidator(0.0001)` |
| `fulfillment_cost_per_1000` | `DecimalField(14, 4)` (L41-48) | `null=True, blank=True` | `MinValueValidator(0)` |
| `minimum_order_quantity` | `PositiveIntegerField` (L50-53) | default `10` | `MinValueValidator(1)` |
| `capacity_percentage` | `DecimalField(5, 2)` (L57-65) | default `70.00` | 0–100 |
| `eligible_level` | `CharField(max_length=30)` (L67-70) | default `"all"` | none (no `choices`; the `EligibleLevel` enum at L11-16 is not attached to the field) |
| `business_share_percentage` | `DecimalField(5, 2)` (L72-80) | default `75.00` | 0–100 |
| `user_reward_percentage` | `DecimalField(5, 2)` (L82-90) | default `25.00` | 0–100 |
| `order_source` | `CharField(max_length=10, choices=api/manual/both)` (L92-96) | default `"both"` | — |
| `target_link_required` | `BooleanField` (L98) | default `True` | — |
| `is_active` | `BooleanField` (L100) | default `True` | — |
| `created_at` / `updated_at` | `DateTimeField(auto_now_add)` / `(auto_now)` (L102-103) | — | — |

- **Relationships:** no outgoing links. Incoming: `Order.service` (`PROTECT`, `related_name="orders"`).
- **Meta (L105-147):** ordered by `-created_at`. Check constraints:
  - `service_price_positive` (price > 0)
  - `service_min_qty_positive` (≥ 1)
  - `service_capacity_pct_range`, `service_business_pct_range`, `service_reward_pct_range` (each 0–100)
  - `service_pct_sum_100`: `business_share_percentage = 100 - F("user_reward_percentage")`
- No unique constraints apart from the primary key, and no custom indexes.
- **Methods:**
  - `clean()` (L149-165) requires `business + reward == Decimal("100.00")`.
  - `save()` (L167-170) calls `self.full_clean()` on **every** save.

### 1.2 `Order` — `orders/models.py:9-150`

| Field | Type | null / blank / default |
|---|---|---|
| `service` | `ForeignKey(Service, on_delete=PROTECT, related_name="orders")` (L31-35) | required |
| `source` | `CharField(10, choices=api/manual)` (L37-41) | default `"api"` |
| `external_order_id` | `CharField(max_length=100, unique=True)` (L45-50) | `null=True, blank=True` |
| `idempotency_key` | `UUIDField(unique=True, editable=False)` (L53) | `null=True, blank=True` |
| `target_link` | `URLField(max_length=2048)` (L55) | no `blank=True`, but `Order.objects.create` does not run model validation |
| `quantity` | `PositiveIntegerField` (L57-59) | validator `MinValueValidator(1)` |
| `price_per_1000_snapshot` | `DecimalField(14, 4)` (L62-66) | `MinValueValidator(0.0001)` |
| `fulfillment_cost_per_1000_snapshot` | `DecimalField(14, 4)` (L67-73) | `null=True, blank=True` |
| `business_share_percentage_snapshot` | `DecimalField(5, 2)` (L75-82) | 0–100 |
| `user_reward_percentage_snapshot` | `DecimalField(5, 2)` (L84-91) | 0–100 |
| `capacity_percentage_snapshot` | `DecimalField(5, 2)` (L93-100) | 0–100 |
| `eligible_user_count_at_acceptance` | `PositiveIntegerField` (L102-104) | default `0` |
| `capacity_limit_at_acceptance` | `PositiveIntegerField` (L106-108) | default `0` |
| `total_order_value` | `DecimalField(max_digits=22, decimal_places=8)` (L111-115) | default `0.00000000` |
| `availability_status` | `CharField(20, choices=available/partially_claimed/fully_claimed/unavailable)` (L117-121) | default `available` |
| `fulfillment_status` | `CharField(20, choices=accepted/in_progress/under_review/completed/cancelled/rejected)` (L123-127) | default `accepted` |
| `created_at` / `updated_at` | (L129-130) | — |

- **Meta (L132-147):** ordered by `-created_at`. Indexes:
  - `order_service_fulfill_idx` (`service`, `fulfillment_status`)
  - `order_avail_status_idx`
  - `order_created_at_idx`
- Unique fields: `external_order_id` and `idempotency_key`. **No check constraints.**
- **Methods:** `__str__` only.

### 1.3 `Task` — `tasks/models.py:6-124`

| Field | Type | null / blank / default |
|---|---|---|
| `order` | `ForeignKey("orders.Order", on_delete=PROTECT, related_name="tasks")` (L19-25) | `null=True, blank=True` |
| `title` | `CharField(150)` (L26) | `blank=True, default=""` |
| `unit_number` | `PositiveIntegerField` (L27) | default `1` |
| `total_slots` | `PositiveIntegerField(editable=False)` (L29) | default `1` |
| `remaining_slots` | `PositiveIntegerField(editable=False)` (L30) | default `1` |
| `admin_request_key` | `UUIDField(unique=True, editable=False)` (L31) | `null=True, blank=True` |
| `admin_request_quantity` | `PositiveIntegerField(editable=False)` (L32) | default `1` |
| `target_link` | `URLField(max_length=2048)` (L35) | required |
| `price_per_1000_snapshot` | `DecimalField(14, 4)` (L36-41) | `null=True, blank=True` |
| `user_reward_percentage_snapshot` | `DecimalField(5, 2)` (L42-47) | `null=True, blank=True` |
| `reward_amount` | `DecimalField(max_digits=18, decimal_places=8)` (L48-52) | default `0` |
| `assigned_user` | `ForeignKey(User, SET_NULL, related_name="assigned_tasks")` (L55-61) | `null=True, blank=True` |
| `status` | `CharField(24, choices=8 values)` (L63-68) | default `available`, `db_index=True` |
| `submission_note`, `review_note` | `TextField` (L71-72) | `blank=True, default=""` |
| `claimed_at`, `submitted_at`, `reviewed_at` | `DateTimeField` (L74-76) | `null=True, blank=True` |
| `reviewed_by` | `ForeignKey(User, SET_NULL, related_name="reviewed_tasks")` (L78-84) | `null=True, blank=True` |
| `created_at` / `updated_at` | (L86-87) | — |

- **Meta (L89-109):** ordered by `id`.
- **Unique constraint `uniq_aggregate_task_per_order` on `order` (L95):** unconditional in code, but the **database still holds the older conditional version** (`order IS NOT NULL`) because migration 0010 is not applied.
- **Check constraints (L96-98):**
  - `task_total_slots_positive` (≥ 1)
  - `task_remaining_nonnegative`
  - `task_remaining_lte_capacity` (`remaining_slots <= total_slots`)
- **Indexes:** `task_order_status_idx` (`order`, `status`) and `task_user_status_idx` (`assigned_user`, `status`).
- **Methods:** the `display_name` property (L114-124) builds a display name only.
- **Legacy fields:** `assigned_user`, `admin_request_key` and `admin_request_quantity` are never written by production code. `unit_number` is always written as `1` (`tasks/services.py:58`).

### 1.4 `TaskClaim` (claim, submission and review) — `tasks/models.py:127-167`

| Field | Type | null / blank / default |
|---|---|---|
| `task` | `ForeignKey(Task, PROTECT, related_name="claims")` (L140) | required |
| `user` | `ForeignKey(User, PROTECT, related_name="task_claims")` (L141) | required |
| `status` | `CharField(24, choices=claimed/work_submitted/post_not_available/approved/rejected/unavailable)` (L142) | default `claimed`, `db_index=True` |
| `submitted_result` | `CharField(24, choices=work_done/post_not_available)` (L143) | `null=True, blank=True` |
| `submission_note`, `review_note` | `TextField` (L144-145) | `blank=True, default=""` |
| `claimed_at` | `DateTimeField(auto_now_add=True)` (L146) | — |
| `target_opened_at` | `DateTimeField` (L147) | `null=True, blank=True` |
| `target_wait_seconds` | `PositiveIntegerField(editable=False)` (L150) | `null=True, blank=True` |
| `submitted_at`, `reviewed_at` | `DateTimeField` (L151-152) | `null=True, blank=True` |
| `reviewed_by` | `ForeignKey(User, SET_NULL, related_name="reviewed_task_claims")` (L153) | `null=True, blank=True` |
| `created_at` / `updated_at` | (L154-155) | — |

- **Meta (L157-164):**
  - Unique constraint `uniq_user_active_task_claim` on `user` with `condition=Q(status="claimed")` (L162). This is a **conditional** constraint, which MySQL does not create.
  - Indexes `task_claim_status_idx` (`task`, `status`) and `user_claim_status_idx` (`user`, `status`).
- **There is no unique constraint on (`task`, `user`) in this table.**

### 1.5 `TaskUserClaim` — `tasks/models.py:170-182`
- **Fields:** `task` (FK → Task, `PROTECT`, `related_name="claiming_users"`), `user` (FK → User, `PROTECT`, `related_name="task_claim_slots"`) and `created_at`.
- **Constraint:** `UniqueConstraint(fields=["task","user"], name="uniq_user_task_claim_identity")` (L178). It is unconditional, so it works on every database.
- **Index:** (`user`, `created_at`).

### 1.6 `TaskStrike` (outcome of a rejection) — `tasks/models.py:185-213`
- **Fields:**
  - `task` (FK, `PROTECT`, `related_name="strikes"`)
  - `claim` (`OneToOneField(TaskClaim, PROTECT, null=True, blank=True, related_name="strike")`, which makes it unique)
  - `user` (FK, `PROTECT`)
  - `issued_by` (FK, `PROTECT`)
  - `reason` (`CharField(500)`)
  - `created_at`
- **Meta:** ordered by `-created_at`. There are no other constraints.

### 1.7 `Wallet` — `wallet/models.py:9-35`
- **Fields:**
  - `user` (`OneToOneField(User, PROTECT, related_name="wallet")`)
  - `balance` (`DecimalField(max_digits=20, decimal_places=8, default=0.00000000)`)
  - `created_at` and `updated_at`
- **Constraint:** `CheckConstraint balance >= 0` (`wallet_balance_nonnegative`).

### 1.8 `WalletTransaction` (ledger) — `wallet/models.py:38-135`

| Field | Type | null / blank / default |
|---|---|---|
| `wallet` | `ForeignKey(Wallet, PROTECT, related_name="transactions")` (L49-53) | required |
| `transaction_type` | `CharField(20, choices=task_reward/payout/adjustment)` (L55-58) | required |
| `direction` | `CharField(6, choices=credit/debit)` (L60-63) | required |
| `amount` | `DecimalField(20, 8)` (L66-69) | required |
| `balance_after` | `DecimalField(20, 8)` (L72-75) | required |
| `task` | `ForeignKey("tasks.Task", PROTECT, related_name="wallet_transactions")` (L78-84) | `null=True, blank=True` |
| `claim` | `ForeignKey("tasks.TaskClaim", PROTECT, related_name="wallet_transactions")` (L85-91) | `null=True, blank=True` |
| `description` | `CharField(255)` (L93-97) | `blank=True, default=""` |
| `created_at` | `auto_now_add` (L99) | — |

- **Meta (L101-129):** ordered by `-created_at, -id`.
- **Check constraints:**
  - `wallet_tx_amount_positive` (> 0)
  - `wallet_tx_balance_nonnegative`
  - `task_reward_requires_task`
- **Unique constraint:** `uniq_claim_reward_ledger_entry` on `claim` with `condition=Q(transaction_type="task_reward")` (L118-122). This is **conditional**, so MySQL does not create it.
- **Index:** `wallet_tx_history_idx` (`wallet`, `created_at`).

### 1.9 `WithdrawalRequest` (payout) — `wallet/models.py:138-208`

| Field | Type | null / blank / default |
|---|---|---|
| `user` | `ForeignKey(User, PROTECT, related_name="withdrawal_requests")` (L148-152) | required |
| `amount` | `DecimalField(20, 8)` (L154-158) | `MinValueValidator(0.00000001)` |
| `payout_method` | `CharField(10, choices=upi)` (L160-163) | required |
| `payout_details` | `CharField(500)` (L165) | required |
| `status` | `CharField(10, choices=pending/approved/rejected)` (L167-172) | default `pending`, `db_index=True` |
| `reviewed_by` | `ForeignKey(User, SET_NULL)` (L174-180) | `null=True, blank=True` |
| `review_note` | `TextField` (L182) | `blank=True, default=""` |
| `requested_at` / `reviewed_at` / `updated_at` | (L184-186) | `reviewed_at` nullable |

- **Meta (L188-205):**
  - Check constraint `withdrawal_amount_positive`.
  - Indexes `withdraw_status_date_idx` (`status`, `requested_at`) and `withdraw_user_status_idx` (`user`, `status`).

### 1.10 `UserLevel` — `accounts/models.py:180-244`
- **Fields:**
  - `code` (`SlugField(30, unique=True)`, L183)
  - `name` (`CharField(50)`)
  - `minimum_approved_tasks` (`PositiveIntegerField`, default `0`, L192)
  - `display_order` (`PositiveSmallIntegerField(unique=True)`, L197)
  - `badge_color` (`CharField(20)`, default `#64748B`)
  - `description`, `benefits`, `extra_earning_description` (each `TextField`, `blank=True`, default `""`)
  - `demo_video_url` (`URLField(500)`, `blank=True`)
  - `strike_limit` (`PositiveIntegerField`, default `3`, validators 1–1000, L212)
  - `is_active` (`BooleanField`, default `True`, L218)
  - `created_at` and `updated_at`
- **Meta (L230-241):**
  - Unique constraint `unique_level_minimum_approved_tasks`.
  - Check constraint `userlevel_strike_limit_range`.
- **Levels are not stored per user.** A user's level is computed from their approved claims (`accounts/strike_policy.py:32-38`).
- `Service.eligible_level` matches `UserLevel.code` as a **plain string, with no foreign key**.

### 1.11 `SiteConfiguration` (payout and claim settings) — `common/models.py`
- **Fields:**
  - `min_withdrawal_amount` (`DecimalField(12, 2)`, default `100.00`, `MinValueValidator(0.01)`, L48-54)
  - `target_wait_seconds` (`PositiveIntegerField`, default `15`, range 1–3600, L59-63)
- **Constraints:** two check constraints (L70-79).
- **Methods:**
  - `save()` forces `pk = 1` (L81-83).
  - `load()` runs `get_or_create(pk=1)` (L85-89).

---

## 2. Service fields: usage map

In the frontend form (`AdminOperations.jsx:46-51`, plus the selects and checkboxes on L64), **every form key matches the model field name exactly.** The serializer is `AdminServiceSerializer` (`services_catalog/serializers.py:9-70`). Its fields are generated by `ModelSerializer`, and the only read-only fields are `service_id`, `created_at` and `updated_at` (L36).

| Model field | Frontend form field | Serializer field (generated) | Every reader (file:line, function) | What it computes |
|---|---|---|---|---|
| `name` | `name` (text, required) | `CharField(max_length=150)`, required, writable | `tasks/models.py:123` `Task.display_name`; `tasks/admin_api_views.py:356` `AdminOrdersAPIView.get`, `:481`/`:496` `AdminTaskHistoryAPIView.get`, `:579` `AdminTaskSubmissionsAPIView.get`; `tasks/views.py:52` `AvailableTasksAPIView.get_queryset` (search); `common/public_views.py:78` `PublicOverviewAPIView.get`; `accounts/admin_views.py:257` `AdminUserTasksAPIView.get`; `orders/views.py:38` `IncomingServicesAPIView.get`; `orders/models.py:150` `Order.__str__` | Display and search only |
| `description` | `description` (textarea) | `CharField`, optional, writable | `tasks/serializers.py:14` `task_description` (TaskSerializer / TaskClaimSerializer); `tasks/views.py:53` (search); `common/public_views.py:79`; `orders/views.py:39` | Display and search only |
| `price_per_1000` | `price_per_1000` ("Selling price per 1,000", required) | `DecimalField(14, 4)`, required, writable | `orders/services.py:140` (snapshot), `:147` (order value); `common/public_views.py:80` (`rate_per_1000`, when `PUBLIC_SERVICE_RATES`); `orders/views.py:40` (`rate`); `services_catalog/calculations.py:6` `calculate_price_per_unit` (called from tests only). Frontend: `AdminWorkflows.jsx:115-116` (estimates), `:199` (display) | Order value; the reward basis (through the snapshot) |
| `fulfillment_cost_per_1000` | `fulfillment_cost_per_1000` (optional) | `DecimalField(14, 4)`, `allow_null=True`, optional, writable | `orders/services.py:141` (snapshot only). Its snapshot is read at `tasks/admin_api_views.py:188-191` `summarize_orders` | The dashboard's fulfillment cost and profit |
| `minimum_order_quantity` | `minimum_order_quantity` (required) | `IntegerField`, optional, writable | `orders/services.py:103-104` (validation); `common/public_views.py:82`; `orders/views.py:42`. Frontend: `AdminWorkflows.jsx:155-156, 200, 208` | The minimum-quantity check |
| `capacity_percentage` | `capacity_percentage` (required) | `DecimalField(5, 2)`, optional, writable | `services_catalog/calculations.py:42-61` `calculate_max_order_quantity` (called from `orders/services.py:127` and `orders/views.py:34`); `orders/services.py:144` (snapshot) | Maximum order quantity |
| `business_share_percentage` | `business_share_percentage` | `DecimalField(5, 2)`, optional, writable | `services_catalog/serializers.py:40-50` (sum check); `services_catalog/models.py:139-165` (constraint and `clean`); `orders/services.py:142` (snapshot); `services_catalog/calculations.py:28-39` `calculate_business_allocation` (**called from tests only**) | **Nothing in production**: it is validated and snapshotted only |
| `user_reward_percentage` | `user_reward_percentage` | `DecimalField(5, 2)`, optional, writable | `orders/services.py:143` (snapshot), then `tasks/services.py:55-61`, then `tasks/calculations.py:7-38`; `services_catalog/calculations.py:17-25` `calculate_user_reward` (**called from tests only**). Frontend: `AdminWorkflows.jsx:116, 202` | The per-claim reward (through the snapshot) |
| `eligible_level` | `eligible_level` (select of active levels plus `all`) | `CharField(max_length=30)`, optional, writable, with `validate_eligible_level` (L10-15) | `orders/services.py:27-39` `get_eligible_user_count`; `orders/views.py:28-34`; `tasks/services.py:31` `user_can_access_task`; `tasks/views.py:37-39` `AvailableTasksAPIView.get_queryset`; `accounts/serializers.py:283` `AdminUserLevelSerializer.validate` | Capacity (the eligible-user count) and task visibility and claim access. **Read live, never snapshotted** |
| `order_source` | `order_source` (select api/manual/both) | `ChoiceField`, optional, writable | `orders/services.py:100-102`; `common/public_views.py:25`; `orders/views.py:22`. Frontend: `AdminWorkflows.jsx:98` | Which order channel is allowed |
| `target_link_required` | `target_link_required` (checkbox) | `BooleanField`, optional, writable | `orders/services.py:105-106`; `orders/views.py:44`. Frontend: `AdminWorkflows.jsx:159, 201, 209` | Target-link validation |
| `is_active` | `is_active` (checkbox) | `BooleanField`, optional, writable | `orders/services.py:98`; `common/public_views.py:24`; `orders/views.py:21`; `accounts/serializers.py:283`; `common/admin_bulk_delete.py:129-131` (archiving sets it to False). Frontend: `AdminWorkflows.jsx:98` | Whether orders are accepted; catalog listing |

---

## 3. Order creation

### 3.1 Paths that create an Order
There is only one `Order.objects.create` in production code: `orders/services.py:133`. Two endpoints reach it.

| Path | Endpoint | View | Permission and auth | Serializer |
|---|---|---|---|---|
| **A. Reseller API** | `POST /api/v1/orders/incoming/` (`orders/urls.py:10-14`) | `IncomingOrderAPIView.post` (`orders/views.py:48-85`) | `HasIncomingOrderAPIKey` (`X-API-Key`), `authentication_classes = []` | `IncomingOrderSerializer` (`orders/serializers.py:4-36`), then `create_api_order` (`orders/services.py:167-176`) |
| **B. Admin manual** | `POST /api/v1/admin/orders/create/` (`orders/urls.py:8`) | `AdminOrderCreateAPIView.post` (`orders/admin_api_views.py:18-39`) | `IsAdminUser` | `AdminCreateOrderSerializer` (`orders/admin_api_views.py:11-15`, `idempotency_key` is a required UUID), then `create_admin_order` (`orders/services.py:179-188`) |
| **Other** | **NOT FOUND.** The Django admin blocks it (`OrderAdmin.has_add_permission` returns `False`, `orders/admin.py`). The only management command is `seed_default_levels`, which creates `UserLevel` rows. | — | — | — |

On the frontend, path B is called through `createAdminOrder` (`admin/api/admin.js:38-44`) from `AdminWorkflows.jsx:162-165`.

### 3.2 Quantity validation
- **Serializer:** both paths use `IntegerField(min_value=1)`. `IncomingOrderSerializer.validate_quantity` also rejects booleans (L30-36).
- **Type and lower bound:** `orders/services.py:75-76`
  ```python
  if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
  ```
- **Minimum:** `orders/services.py:103-104`
  ```python
  if quantity < service.minimum_order_quantity:
  ```
- **Maximum:** `orders/services.py:126-129` rejects the order when `quantity > capacity_limit`. The limit comes from `services_catalog/calculations.py:54-61`:
  ```python
  capacity = (Decimal(eligible_user_count) * capacity_percentage / Decimal("100"))
  return int(capacity.to_integral_value(rounding=ROUND_FLOOR))
  ```
  The eligible-user count (`orders/services.py:15-40`) counts active, approved, non-staff users. For a level-restricted service it counts only users whose approved-claim count is at least the level's `minimum_approved_tasks`.
- **Frontend:** checks for a whole number at or above the minimum (`AdminWorkflows.jsx:155-158`). There is no maximum check in the browser.

### 3.3 Order amount formula
`orders/services.py:147`:
```python
total_order_value=service.price_per_1000 * quantity / 1000,
```
- **Types:** a `Decimal` (14,4) times an `int`, divided by an `int`, giving a `Decimal` in Python's default context (28 significant digits).
- **Rounding:** none explicit. A price with at most 4 decimal places divided by 1000 has at most 7 decimal places, so the value fits the 8-decimal field exactly. The database check confirmed `0` mismatches between `total_order_value` and `price_snapshot × quantity / 1000`.
- **On save:** Django 5.2 passes the Decimal through unchanged (`BaseDatabaseOperations.adapt_decimalfield_value` returns `value`).
- **SQLite storage:** the value is sent as text and stored with NUMERIC affinity, which SQLite can convert to REAL. The current database has 5 REAL and 3 INTEGER values in this column. On read, Django converts through a float with `prec=15` and then quantizes (`django/db/backends/sqlite3/operations.py:335-356`).
- **MySQL:** `DECIMAL(22,8)` is exact.

### 3.4 Values snapshotted vs read live
Copied onto the Order at creation (`orders/services.py:133-150`):
- `price_per_1000_snapshot`
- `fulfillment_cost_per_1000_snapshot`
- `business_share_percentage_snapshot`
- `user_reward_percentage_snapshot`
- `capacity_percentage_snapshot`
- `eligible_user_count_at_acceptance`
- `capacity_limit_at_acceptance`
- `total_order_value`

The Task then copies `price_per_1000_snapshot` and `user_reward_percentage_snapshot` **from the Order** and computes `reward_amount` from them (`tasks/services.py:54-61`).

Read **live** from `Service` after the order exists:
- `name` and `description`, for display.
- **`eligible_level`**, which controls task access (`tasks/services.py:31`, `tasks/views.py:37-39`). It is never snapshotted.
- `is_active`, `order_source`, `target_link_required` and `minimum_order_quantity` are checked only at creation and on retries.

### 3.5 Tasks created per order
Exactly **one** Task per order is created, in `create_tasks_for_order` (`tasks/services.py:35-62`):
```python
Task.objects.create(order=order, unit_number=1, total_slots=order.quantity,
    remaining_slots=order.quantity, target_link=order.target_link, ...
    user_reward_percentage_snapshot=reward_percentage, reward_amount=reward)
```
- **Capacity:** `total_slots = remaining_slots = order.quantity`.
- **Retry:** if the order already has one Task with `total_slots == quantity`, the function returns without creating another. Any other existing state raises a `ValidationError` (L44-47).
- **Database:** `uniq_aggregate_task_per_order` (see 1.3).

### 3.6 Transactions and atomicity
- `_create_service_order` runs inside `@transaction.atomic` (L73). The Service row is locked with `select_for_update()` (L95).
- The Order insert runs in a nested savepoint (L131-150). If the insert hits an `IntegrityError` on `external_order_id` or `idempotency_key`, the existing identical order is returned (L151-160).
- **Idempotency:** a repeated `external_order_id` or `idempotency_key` with the same details returns the existing order. The same key with different details is rejected (L115-124).
- `create_tasks_for_order` (also `@transaction.atomic`, with `select_for_update` on the order at L39) runs inside the same outer transaction (L162-163). If Task creation fails, the Order is rolled back too.
- `ATOMIC_REQUESTS` is not set: **NOT FOUND**.
- **Responses:** the API path always returns 200. The admin path returns 201 for a new order and 200 for an existing one.

---

## 4. Task, Claim and Submission flow

### 4.1 Claim capacity
- **Initialised** at Task creation: `total_slots = remaining_slots = order.quantity` (`tasks/services.py:58-59`).
- **Decremented** atomically in `_claim_task_atomic` (`tasks/services.py:110-113`):
  ```python
  changed = Task.objects.filter(pk=task_id, status=Task.Status.AVAILABLE, remaining_slots__gt=0).update(
      remaining_slots=F("remaining_slots") - 1, updated_at=now)
  if not changed:
      raise ValidationError("This task is no longer available.")
  ```
- **Exhaustion** (L118-128): when `remaining_slots == 0`, the Task becomes `COMPLETED` and the Order becomes `fulfillment_status=COMPLETED, availability_status=FULLY_CLAIMED`.
- **Slots are never given back.** No code increments `remaining_slots`. A rejection or a "Post Not Available" result does not free the slot.
- `confirm_order_target_unavailable` (L366-403) sets `remaining_slots=0`, but **no URL routes to it** (NOT FOUND in any `urls.py`).

### 4.2 Stopping the same user from claiming a task twice
- **Database:** `TaskUserClaim` has a unique constraint on (`task`, `user`) (`tasks/models.py:178`). It is unconditional, so it holds on every database. Inserting that row (L116) and the claim (L117) happens in the same transaction, so an `IntegrityError` rolls back the slot decrement.
- **Code:** checks at `claim_task` L72-73 and `_claim_task_atomic` L103-104, with `IntegrityError` mapped to friendly messages (L77-82).
- **One active claim per user at a time:** a conditional unique constraint (`tasks/models.py:162`, SQLite and PostgreSQL only), a code check (L105-106) and a row lock on the user (L98).
- `TaskClaim` itself has no (`task`, `user`) unique constraint. The database holds 2 (`task`, `user`) pairs with more than one claim (Section 9, legacy data).

### 4.3 Race-condition protection
| Mechanism | Where |
|---|---|
| Conditional `UPDATE` with `F()` to consume a slot | `tasks/services.py:110-111` |
| `select_for_update()` on the user row while claiming | `tasks/services.py:98` (ignored by SQLite, which serializes writes instead) |
| Retry on SQLite "database is locked" errors | `tasks/services.py:83-86` (claims), `272-284` (reviews) |
| `select_for_update()` on the Service and Order rows when creating orders and tasks | `orders/services.py:95`, `tasks/services.py:39` |
| Status-guarded conditional `UPDATE` on submit (`status=claimed`) | `tasks/services.py:232-238` |
| Status-guarded conditional `UPDATE` on review (`status=work_submitted`) | `tasks/services.py:307-313`, `338-344` |
| `select_for_update()` on the user before a strike | `tasks/services.py:352` and `accounts/strike_policy.py:82` |

### 4.4 Status transitions
**Task** (`tasks/models.py:8-16`):
- Starts `AVAILABLE` and becomes **`COMPLETED`** when its last slot is claimed (`tasks/services.py:121`). This happens inside the claiming user's request.
- Becomes `UNAVAILABLE` only through `confirm_order_target_unavailable`, which is unreachable (no URL).
- The statuses `CLAIMED`, `WORK_SUBMITTED`, `POST_NOT_AVAILABLE`, `APPROVED` and `REJECTED` are **never set** by production code.

**Order `fulfillment_status`:**
- `ACCEPTED` at creation (`orders/services.py:149`).
- `IN_PROGRESS` at the first claim when slots remain (`tasks/services.py:130-131`).
- **`COMPLETED`** when the last slot is claimed (`tasks/services.py:124-128`).
- `CANCELLED` when an admin bulk-deletes the order's Task (`common/admin_bulk_delete.py:115-117`).
- `UNDER_REVIEW` and `REJECTED` are **never set** (NOT FOUND).

**Order `availability_status`:**
- `AVAILABLE`, then `PARTIALLY_CLAIMED` (L132-133), then `FULLY_CLAIMED` (L126).
- `UNAVAILABLE` after a bulk Task delete (`common/admin_bulk_delete.py:116`).

**Who sets Completed:** the claim of the last slot, not a reviewer. "Completed" means every slot is claimed, not that work was approved (comment at `tasks/services.py:119-120`). An Order is never reverted if its claims are later rejected.

### 4.5 Claims, submissions and reviews (all stored on `TaskClaim`)
1. **Claim:** `POST /api/v1/tasks/<task_id>/claim/` creates the claim with status `CLAIMED` (`tasks/services.py:117`).
2. **Open target:** `POST /api/v1/tasks/<task_id>/open-target/` records `target_opened_at` and `target_wait_seconds` on the first open only (`tasks/services.py:189-190`).
3. **Submit:** `POST /api/v1/tasks/<task_id>/submit/` with `result` set to `work_done` (status becomes `WORK_SUBMITTED`) or `post_not_available` (status becomes `POST_NOT_AVAILABLE`) (`tasks/services.py:220-238`). The server rejects the submission until the wait has passed (L228-231).
4. **Review:** `POST /api/v1/admin/task-submissions/<claim_id>/review/` with `{"action": "approve" | "reject"}` (`tasks/admin_api_views.py:78-112`). The Task-level route `POST /api/v1/admin/tasks/<task_id>/review/` works only when exactly one submission is pending; otherwise it returns 409 (L72-76).
   - **Approve:** `WORK_SUBMITTED` becomes `APPROVED` (L307-313) and the reward is credited (L319).
   - **Reject:** `WORK_SUBMITTED` becomes `REJECTED` (L338-344). A `TaskStrike` is created (L355-361), then `enforce_strike_limit` runs (L362; `accounts/strike_policy.py:74-100`). The view passes no note (`tasks/admin_api_views.py:86-96`), so the strike `reason` is `""`.
   - **Post Not Available:** a dead end. Approve and reject only accept `WORK_SUBMITTED` (L307, L338), so no reward or strike is possible and the slot stays consumed. The admin listing groups it as "reviewed" (`tasks/admin_api_views.py:534-541`).
   - Claim status `UNAVAILABLE` is never set; it is only counted (`accounts/admin_views.py:185`).

---

## 5. Rewards and wallet

### 5.1 Reward per approved claim
`tasks/calculations.py:4, 31-38`:
```python
REWARD_DECIMAL_PLACES = Decimal("0.00000001")
reward = (price / Decimal("1000")) * (percentage / Decimal("100"))
return reward.quantize(REWARD_DECIMAL_PLACES, rounding=ROUND_HALF_UP)
```
- **Inputs:** the Order snapshots (`tasks/services.py:54-57`). The result is stored once as `Task.reward_amount` (`DecimalField(18, 8)`).
- **Credit:** each approved claim is credited exactly `task.reward_amount` (`wallet/services.py:49`).
- **Example** from the docstring (L11-14): price ₹100 at 25% gives ₹0.025. Sample order 1 in Section 9 matches.

### 5.2 Where Business Share % and User Reward % are used
- **`user_reward_percentage`:** snapshotted at order creation, then used in `calculate_task_reward` (above). This is its only production use.
- **`business_share_percentage`:** validated (`services_catalog/serializers.py:38-52`, `services_catalog/models.py:139-165`) and snapshotted (`orders/services.py:142`). **It is used in no production calculation.**
  - `calculate_business_allocation` (`services_catalog/calculations.py:28-39`) and `calculate_user_reward` (L17-25) are called only from `services_catalog/tests.py`.
  - The dashboard's profit figure does not use it (see 6.1).

### 5.3 When the wallet is credited, and which rows are written
Credit happens when an admin approves the claim, inside the approval transaction: `_approve_submitted_task` calls `credit_approved_task_reward(claim_id=...)` (`tasks/services.py:319`). Rows written:
1. `TaskClaim` is updated to `status=approved` with `reviewed_by` and `reviewed_at` (`tasks/services.py:307-313`).
2. A `Wallet` is created if missing (`wallet/services.py:18-24`, `select_for_update().get_or_create`).
3. A `WalletTransaction` is inserted with `task_reward`, `credit`, `amount=task.reward_amount`, `balance_after`, `task`, `claim` and `description` (`wallet/services.py:74-83`).
4. `Wallet.balance` is set to `balance + amount` (`wallet/services.py:67, 95-96`).

### 5.4 Protection against crediting the same claim twice
1. **Status guard:** approval only works from `WORK_SUBMITTED`, through a conditional `UPDATE` (`tasks/services.py:307-317`), so a claim can be approved only once.
2. **Row locks:** `select_for_update()` on the claim (`wallet/services.py:38`), the user (L56) and the wallet (L20).
3. **Existing-credit check:** a lookup for an existing `task_reward` row on this claim (`wallet/services.py:59-65`).
4. **Database constraint:** `uniq_claim_reward_ledger_entry` (`wallet/models.py:118-122`). It is **conditional**, so it is enforced on SQLite and not created on MySQL.
5. **Fallback:** if the insert hits an `IntegrityError`, the existing row is returned instead (`wallet/services.py:84-93`).
6. **Legacy caveat:** one reward credit has `claim = NULL`, on Task 1, and approved claim 1 (Task 1) has no credit linked to the claim (Section 9). The check in item 3 could not see that payment.

### 5.5 Cap on total rewards per order
- **An explicit check that credited rewards stay within `total_order_value × reward%`: NOT FOUND.**
- **Implicit cap:** each claim consumes one slot (4.1), and each claim is credited at most once (5.4). Total credits for an order are therefore at most `quantity × reward_amount`.
- Because `reward_amount` is rounded half-up to 8 decimal places, `quantity × reward_amount` can exceed `total_order_value × reward% / 100` by up to `quantity × 0.000000005`.
- Standalone tasks (no order) have **no budget at all**.
- The database check found no order whose credited rewards exceed its budget (Section 9).

### 5.6 Payout flow
- **Request:** `create_withdrawal_request` (`wallet/services.py:110-194`) checks the minimum (`SiteConfiguration.min_withdrawal_amount`, L131-136) and the balance (L169-170). It then writes a `payout` debit ledger row (L181-187) and reduces `Wallet.balance` straight away (L190-191).
- **Rejection:** `review_withdrawal_request` (L197-) writes a `payout` credit (refund) row and restores the balance (from L225).

---

## 6. Profit and dashboard

### 6.1 `GET /api/v1/admin/dashboard/stats/` — `AdminDashboardStatsAPIView` (`tasks/admin_api_views.py:155-250`), `IsAdminUser`
Query parameters are `date_from` and `date_to` (`YYYY-MM-DD`). Both default to `timezone.localdate()` (L166-172).

**Counts** (all Orders, **cancelled ones included**):
```python
order_totals = Order.objects.aggregate(total=Count("id"),
    today=Count("id", filter=Q(created_at__gte=today_start, created_at__lt=tomorrow_start)),
    quantity=Sum("quantity"))                                    # L211-215
task_totals = Task.objects.aggregate(total=Count("id"), available=Count("id", filter=available),
    remaining_capacity=Sum("remaining_slots", filter=available))  # L216-221
"in_progress_claims": TaskClaim.objects.filter(status=TaskClaim.Status.CLAIMED).count()  # L235
```

**Financials:** `summarize_orders` (L183-207) is applied to today's orders and to the selected range. It excludes `CANCELLED` and `REJECTED` orders (L185-187):
```python
rows = list(orders.values_list("total_order_value", "fulfillment_cost_per_1000_snapshot", "quantity"))  # L188
revenue = sum((value for value, _cost, _qty in rows), Decimal("0"))                                       # L189
cost_known = all(cost is not None for _value, cost, _qty in rows)                                       # L190
cost = sum((cost * qty / Decimal("1000") for _value, cost, qty in rows if cost is not None), Decimal("0"))  # L191
rewards = WalletTransaction.objects.filter(task__order__in=orders, transaction_type=TASK_REWARD,
    direction=CREDIT).aggregate(total=Sum("amount"))["total"] or Decimal("0")                           # L194-198
"actual_profit": str(revenue - cost - rewards) if cost_known else None                                  # L205
```
- **Uses Order-stored values only** (`total_order_value` and `fulfillment_cost_per_1000_snapshot`). **Current Service prices are not read.**
- Revenue and cost are summed in Python Decimals. Rewards use a database `SUM`, which on SQLite adds the stored REAL values as floats.
- Rewards are attributed to an order regardless of when they were credited. Rewards on **standalone tasks** (no order) and `adjustment` credits never appear in any financial figure.

### 6.2 Other aggregation endpoints
| URL | View | Aggregation | Price source |
|---|---|---|---|
| `GET /api/v1/public/overview/` (cached 60s, AllowAny) | `PublicOverviewAPIView` (`common/public_views.py:30-`) | Count and Sum of open tasks and slots (L46-48); approved-claim count (L62); approved-worker count (L63-68) | **Live** `service.price_per_1000` (L80) |
| `GET /api/v1/tasks/my/summary/` | `MyTaskSummaryAPIView` (`tasks/views.py:152-176`) | Claim counts by status (L158-161); `Sum(amount)` of the user's `task_reward` credits (L162-166) | Ledger |
| `GET /api/v1/admin/users/<id>/activity/` | `AdminUserActivityAPIView` (`accounts/admin_views.py`) | Conditional `Count` per claim status (L175-189); `Sum(amount)` of `task_reward` credits (L192-198) | Ledger |
| `GET /api/v1/admin/tasks/history/` | `AdminTaskHistoryAPIView` (`tasks/admin_api_views.py`) | Per-task `Count(..., distinct=True)` annotations (L430-434); `allocated_order_value_per_claim = order.total_order_value / task.total_slots` (L509-512) | Order-stored value |
| `GET /api/v1/admin/tasks/<id>/submissions/` | `AdminTaskSubmissionsAPIView` | Claim counts by review group (L534-541, plus the grouped count) | — |
| `GET /api/v1/services/incoming/` | `IncomingServicesAPIView` (`orders/views.py:15-45`) | `maximum_quantity` from **live** capacity | **Live** Service values |

### 6.3 Frontend dashboards
- **Admin:** `src/admin/AdminDashboard.jsx` loads data through `getAdminDashboardStats` (`admin/api/admin.js:3-7`) and validates the response with `normalizeAdminDashboardStats` (L218-237). The first load (`AdminApp.jsx:151`) sends no dates, so the backend uses **today**.
  - **Financial Overview** (from `financials.selected_range`, L71, L88-90): Order Revenue (`order_revenue`), Actual Fulfillment Cost (`fulfillment_cost`) and Actual Profit (`actual_profit`), with date pickers (L94-96).
  - **Order Overview** (L100-104): `orders.total`, `orders.today`, `orders.total_quantity`.
  - **Task Overview** (L106-110): `tasks.total`, `tasks.available` (with a `remaining_capacity` hint) and `tasks.in_progress_claims`.
  - **Returned but not displayed:** `rewards_credited`, `order_count`, `order_quantity`, the whole `financials.today` block, and `profit_formula`.
  - **Formatting:** `formatCurrency` uses `Number(value)` and `Intl.NumberFormat` with at most 2 decimal places (L19-24).
- **Worker:** `src/Dashboard.jsx:11-16` loads `GET /api/v1/wallet/` (balance), `GET /api/v1/tasks/my/summary/` (`total_earned`, `approved`, `pending_review`, `rejected`), the user's level and recent history (L126-130).

### 6.4 Timezone and date filters
- `TIME_ZONE = "Asia/Kolkata"` (`config/settings.py:166`) and `USE_TZ = True` (L170). The database stores UTC.
- **Date filters** convert a local date to aware datetimes in the current timezone, as a half-open range `[day 00:00, next day 00:00)`:
  ```python
  timezone.make_aware(datetime.combine(first_day, time.min)),
  timezone.make_aware(datetime.combine(last_day + timedelta(days=1), time.min)),   # tasks/admin_api_views.py:176-177
  ```
- The same pattern appears in Admin Orders (`tasks/admin_api_views.py:308-323`), Admin Task History (L469-471) and Admin Wallet Transactions (`wallet/views.py:171-182`).
- The dashboard response **hard-codes** `"timezone": "Asia/Kolkata"` (L239) instead of reading it from settings.

---

## 7. Service edit bug

### 7.1 Backend
- **Endpoint:** `PATCH /api/v1/admin/services/<int:service_id>/` (`services_catalog/urls.py:9`). It is handled by `AdminServiceDetailAPIView.patch` (`services_catalog/admin_api_views.py:45-49`) with `IsAdminUser` and `partial=True`. `GET` also exists. `PUT` is not implemented (405), but the frontend uses PATCH.
- **Serializer:** `AdminServiceSerializer` (`services_catalog/serializers.py`).
  - Read-only fields: `service_id`, `created_at`, `updated_at` (L36).
  - `validate_eligible_level` requires `"all"` or an **active** `UserLevel.code` (L10-15).
  - `validate` requires business + reward = 100 (L38-52).
  - On save, `Service.save()` runs `full_clean()` (`models.py:167-170`), and any model errors become a 400 (L54-62).

### 7.2 Frontend
- **The form exists:** `AdminServices` in `src/admin/AdminOperations.jsx:53-65`.
- **Loading existing values:** `edit(item)` copies the **list row** from `GET /api/v1/admin/services/` straight into the form; there is no detail request (L60):
  ```js
  function edit(item) { setEditing(item); setForm({ ...serviceEmpty, ...item }); ... }
  ```
  Inputs render `value={form[key] ?? ''}` (L64), so a `null` value **looks** empty in the form.
- **The request:** `updateAdminService` sends `PATCH /api/v1/admin/services/{id}/` with a JSON body (`admin/api/admin.js:174-176`). The payload is built at L61:
  ```js
  fulfillment_cost_per_1000: form.fulfillment_cost_per_1000 === '' ? null : String(form.fulfillment_cost_per_1000),
  ```
- **Field names:** all 12 editable payload keys match the serializer names. The extra keys (`service_id`, `created_at`, `updated_at`) are read-only, and DRF ignores them.

### 7.3 Most likely cause (reproduced)
**For a service whose `fulfillment_cost_per_1000` is `null`, the form value is `null`, not `''`.**
- The `=== ''` test is false, so `String(null)` sends the text **`"null"`**.
- DRF's `DecimalField` rejects it with **`400 {"fulfillment_cost_per_1000": ["A valid number is required."]}`**.
- `errorText()` (L29-37) displays only "A valid number is required." in the modal, without naming the field.
- Creating a service works, because `serviceEmpty.fulfillment_cost_per_1000` is `''`.

Reproduction: the exact frontend payload for each service was run through `is_valid()` and `full_clean()` in memory, with nothing saved.

| Service ID | Stored cost | Edit with no changes | Edit the name only | Cost field cleared to `''` | Cost typed as `5` |
|---|---|---|---|---|---|
| 1 | `null` | ❌ `A valid number is required.` | ❌ same | ✅ valid | ✅ valid |
| 2 | `null` | ❌ same | ❌ same | ✅ valid | ✅ valid |
| 5 | `null` | ❌ same | ❌ same | ✅ valid | ✅ valid |

**All 3 services in the database have a `null` cost, so every edit fails** unless the admin types a cost (or types something and deletes it). The cause is in the frontend payload, so it predates this session's backend changes; the serializer's handling of `"null"` was not changed.

### 7.4 Other possible causes, not triggered by the current data
- The **Save button is disabled when `levels.length === 0`** (L64). If no level is active, or the levels request fails, no service can be saved. The database has 4 active levels.
- The eligible-level `<select>` lists only active levels (L59, L64). A service pointing at an inactive or missing level keeps that value in state, and the backend rejects it with "Choose an active configured user level." The database has 0 such services.
- `Service.save()` always runs `full_clean()`, so any stored invalid value would block edits. In the reproduction, `full_clean()` passed once the cost value was valid.

---

## 8. Duplications and risks

### 8.1 The same calculation in more than one place
| Calculation | Locations |
|---|---|
| Order value | `orders/services.py:147` (used); `services_catalog/calculations.py:9-14` `calculate_order_value` (tests only); `AdminWorkflows.jsx:115` (JS float estimate) |
| Reward per claim | `tasks/calculations.py:31-38` (used; rounded half-up to 8 places); `services_catalog/calculations.py:17-25` `calculate_user_reward` (tests only, **no rounding**); `AdminWorkflows.jsx:116` (JS float, no rounding) |
| Business allocation | `services_catalog/calculations.py:28-39` only (tests only); the dashboard profit uses a different formula (6.1) |
| User level from approved claims | `accounts/strike_policy.py:18-38`; `tasks/services.py:23-25` `get_accessible_level_codes`; `orders/services.py:30-39`; `tasks/views.py:36-39` |
| Business % + reward % = 100 | `services_catalog/models.py:139-146` (constraint), `:149-165` (`clean`), `services_catalog/serializers.py:38-52` (consistent, but triplicated) |
| Sum of credited rewards (`task_reward` + `credit`) | `tasks/admin_api_views.py:194-198`; `tasks/views.py:162-166`; `accounts/admin_views.py:192-196` |
| Pagination classes (page size 20, max 100) | Declared separately 11 times (`max_page_size = 100`) |

### 8.2 Floating-point money
- **Backend:** no `FloatField` and no `float()` on money (NOT FOUND). All money uses `DecimalField`.
- **SQLite storage:** Decimals are stored as REAL or INTEGER and read back through a 15-significant-digit float conversion (Section 9 shows the storage classes). Database `SUM` on these columns adds floats. MySQL `DECIMAL` is exact.
- **Frontend:**
  - `Number()` for the order and reward estimates (`AdminWorkflows.jsx:115-116`).
  - `Number()` for display (`AdminDashboard.jsx:19-24`, `lib/format.js:27-34`, `AdminOrders.jsx:52`, `AdminWalletTransactions.jsx:52`, `admin/withdrawalIdentity.js:40`).
  - The withdrawal check is `Number(amount) > 0` only (`lib/withdrawalValidation.js:9`); the server checks the balance.

### 8.3 Missing snapshots
- `eligible_level` is not snapshotted on Order or Task, so access to existing tasks follows the **current** Service setting (`tasks/services.py:31`, `tasks/views.py:37-39`).
- Service `name` and `description` are read live for task display.
- The 3 standalone Tasks have `price_per_1000_snapshot` and `user_reward_percentage_snapshot` set to `NULL`. Their `reward_amount` values (13, 3333 and 100) are not derived from any pricing.

### 8.4 Missing database constraints
- **Order:** no check constraints. `quantity >= 1` is only a validator; the database enforces just `>= 0` through `PositiveIntegerField`. `total_order_value >= 0` and the percentage ranges are not enforced in the database.
- **Task:** `reward_amount > 0` is not enforced (it is checked only at credit time, `wallet/services.py:51-52`). `total_slots == order.quantity` is code-only (`tasks/services.py:44-47`).
- **TaskClaim:** no unique (`task`, `user`); this relies on the separate `TaskUserClaim` table.
- **Service:** `eligible_level` is a free string with no foreign key to `UserLevel.code`.
- **Wallet:** `balance` is a denormalized total, with no constraint tying it to the ledger. All 5 wallets reconcile today.
- **Conditional unique constraints that MySQL does not create:**
  - `uniq_user_active_task_claim`
  - `uniq_claim_reward_ledger_entry`
  - `uniq_aggregate_task_per_order`: the code version is unconditional, but migration 0010 is not applied, so the database still has the conditional one.

### 8.5 Places where totals could be double-counted or wrong
- **Dashboard rewards:** each ledger row joins to exactly one Task and Order, so it is counted once. The per-claim uniqueness behind this is conditional (not enforced on MySQL).
- **Legacy reward credit:** the reward with `claim = NULL` on Task 1 is invisible to the per-claim duplicate check. Today the status guard blocks a second credit for claim 1.
- **Revenue:** recognised at order creation. An Order becomes `COMPLETED` once every slot is claimed, even if all its submissions are rejected or Post Not Available. Only `CANCELLED` and `REJECTED` are excluded, and `REJECTED` is never set.
- **Two different order totals on one dashboard:** `orders.total` and `total_quantity` **include** cancelled orders (L211-215), while financials **exclude** them (L185-187).
- **Unattributed credits:** rewards on standalone tasks (₹3,533.00) and `adjustment` credits (₹901.00) appear in no profit figure.
- **Public overview:** `approved_submissions` (`common/public_views.py:62`) also counts claims on standalone tasks.
- **Inconsistent order statuses:** 5 orders have `fulfillment_status=completed` but `availability_status=available` (legacy data).
- **Lost capacity:** a `POST_NOT_AVAILABLE` claim consumes its slot permanently, and no admin action can act on it.
- **UPI rule mismatch:** the frontend's `validateUpiId` (`lib/withdrawalValidation.js:13-20`) accepts `name@bank.com`, but the backend's `UPI_ID_PATTERN` (`wallet/services.py:15`, added earlier this session) rejects it. The user then sees the server's error.

---

## 9. Aggregate data snapshot (read-only; counts, sums and IDs only)

### 9.1 Services and levels
| Metric | Value |
|---|---|
| Services | 3, all `is_active = False` |
| `order_source` | both: 3 |
| `eligible_level` | all: 3 |
| `fulfillment_cost_per_1000` NULL | 3 |
| User levels | 4, all active (bronze, silver, gold, diamond) |
| Services pointing at a missing or inactive level | 0 |

### 9.2 Orders
| Metric | Value |
|---|---|
| Orders | 8 (api: 2, manual: 6) |
| `fulfillment_status` | completed: 7, in_progress: 1 |
| `availability_status` | available: 5, fully_claimed: 2, partially_claimed: 1 |
| fulfillment × availability | (completed, available): 5 · (completed, fully_claimed): 2 · (in_progress, partially_claimed): 1 |
| Cost snapshot NULL | 8 |
| **Sum of `total_order_value`** | **9.00000000** (the same with cancelled and rejected excluded) |
| Orders where stored value ≠ price snapshot × qty / 1000 | 0 |
| SQLite storage class of `total_order_value` | real: 5, integer: 3 |

### 9.3 Tasks
| Metric | Value |
|---|---|
| Tasks | 11 (order-backed: 8, standalone: 3) |
| `status` | completed: 10, available: 1 |
| Orders with more than 1 Task / with 0 Tasks | 0 / 0 |
| Tasks where `total_slots` ≠ `order.quantity` | 0 |
| Tasks with more claims than `total_slots` | 0 |
| Tasks where claims ≠ `total_slots − remaining_slots` | 0 |
| Tasks where `reward_amount` ≠ `calculate_task_reward(snapshots)` | 0 |
| Tasks missing price or reward-% snapshots | 3 / 3 (the standalone Tasks) |

### 9.4 Claims (`TaskClaim`: claim, submission and review)
| Metric | Value |
|---|---|
| Claims | 18 |
| `status` | approved: 10, rejected: 6, work_submitted: 1, post_not_available: 1 |
| Submitted (`submitted_at` set), by status | approved: 10, rejected: 6, work_submitted: 1, post_not_available: 1 (all 18) |
| `submitted_result` | NULL: 9 (legacy: approved 7, rejected 2), work_done: 8, post_not_available: 1 |
| `TaskUserClaim` rows | 14; every claim has a matching row |
| (`task`, `user`) pairs with more than 1 claim | 2 pairs, 4 extra claims, on Task IDs 4 and 7 (legacy) |
| Strikes / rejected claims | 6 / 6 |

### 9.5 Wallet and ledger
| Metric | Value |
|---|---|
| Ledger rows | task_reward/credit: 10 · payout/debit: 3 · payout/credit (refund): 1 · adjustment/credit: 3 |
| **Sum of reward credits** | **3533.65000000**: order-backed Tasks 0.65000000 (7 credits), standalone Tasks 3533.00000000 (3 credits) |
| Adjustment credits | 901.00000000 (3 rows) |
| Claims credited more than once | 0 |
| Approved claims with no claim-linked credit | 1 (claim ID 1, paid through the legacy NULL-claim credit on Task 1, amount 0.02500000) |
| Credits on non-approved claims | 0 |
| Credits whose amount ≠ `task.reward_amount` | 0 |
| Wallets / wallets where balance ≠ credits − debits | 5 / 0 |
| Sum of wallet balances | 4184.65000000 |
| Withdrawals | approved: 2, rejected: 1; total amount 400.00000000 |
| Orders whose credited rewards exceed `order_value × reward%` | 0 |
| SQLite storage class of ledger `amount` / wallet `balance` | real 7, integer 10 / real 3, integer 2 |

### 9.6 Three sample orders
| Order | Quantity | Stored amount | Status | Task | Capacity | Remaining | Reward / claim | Claims | Approved | Total credited |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 0.10000000 | completed | 1 | 1 | 0 | 0.02500000 | 1 | 1 | 0.02500000 |
| 6 | 1 | 2.00000000 | completed | 11 | 1 | 0 | 0.50000000 | 1 | 1 | 0.50000000 |
| 9 | 2 | 2.00000000 | in_progress | 17 | 2 | 1 | 0.25000000 | 1 | 0 | 0 |

*No usernames, emails, phone numbers, UPI IDs or links were read into this report.*
