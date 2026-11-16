# Engineering Report

## Findings and priorities

I measured before changing anything. A DEBUG-only middleware
(`catalog/middleware.py`) adds `X-DB-Query-Count` and `Server-Timing` headers to
every response and logs the most-repeated SQL statement per request. A
management command (`measure_performance`) replays each screen's request
sequence against the seeded dataset.

The four problems that mattered, in priority order:

1. **The catalogue fetched every listed product's detail, one request at a time**
   (`CatalogueView.vue`). Each page made 26 sequential requests, and the list
   response already contained every field it fetched.
2. **The editor ran two queries per variant** (prices and inventory in a loop in
   `ProductEditorView`), so its cost grew with the variant count.
3. **The list serializer read `category.name` without `select_related`**, adding
   one query per row.
4. **The editor made a second request only to get `image_url`.**

Investigated and deliberately left alone:

- **Search indexes:** `icontains` becomes `LIKE '%term%'`, which a B-tree index
  cannot use, and search showed no measured problem.
- **List ordering and count:** already served by the `merchant_id` index.
- **The activity index:** the seeded data contains no audit rows, so there is no
  evidence yet.
- **The fixed per-request session, auth, and membership queries.**

## Performance evidence

The command uses the same dataset (`./setup.sh`: 5000 products, seed 42), the
same screens and products, 3 warm-up runs and 20 measured runs, and reports the
median and p95. Raw output is in `backend/perf/baseline.md` (unmodified code)
and `backend/perf/after.md`.

| Screen | Requests | Queries | Median time |
|---|---:|---:|---:|
| Catalogue page 1 | 26 → 1 | 130 → 5 | 522 → 21 ms |
| Catalogue search | 26 → 1 | 130 → 5 | 475 → 31 ms |
| Editor, 50 variants | 2 → 1 | 110 → 7 | 301 → 34 ms |
| Editor, 2 variants | 2 → 1 | 14 → 7 | 45 → 21 ms |
| Activity (unchanged) | 1 | 5 | 39 → 17 ms |

To reproduce, run `./setup.sh`, then:

```bash
docker compose exec --user "$(id -u):$(id -g)" backend \
  python manage.py measure_performance --output perf/<name>.md
```

The command refuses to overwrite an existing file. `--user` keeps the output
file owned by you rather than root.

Notes on the method:

- **The timings are in-process server time.** They exclude network and browser
  cost, so they understate the gain from removing 25 sequential browser round
  trips.
- **Query counts are exact and repeatable; timings vary between runs.** The
  activity screen got faster without any change to it.
- **The replay follows the frontend.** After the fix the screens send fewer
  requests, which is itself the fix. The dataset, scenarios, and statistics are
  the same.
- **`assertNumQueries` tests guard the fixed counts.** The list query count stays
  the same with 1 or 10 products, and the editor's with 1 or 3 variants. These
  tests failed against the old code.

## Implementation

**Performance:**

- `select_related("category")` on the list.
- The editor loads variants with `select_related("inventory")` plus an ordered
  `Prefetch` of prices. The response shape is unchanged, including
  `inventory: null`, and `image_url` was added.
- The frontend uses the responses it already receives.
- I rejected rewriting the editor as nested serializers: it gives the same query
  count with more contract risk.

**Inventory adjustments:**

- The endpoint is
  `POST /api/products/<id>/variants/<variant_id>/inventory-adjustments/?merchant_id=N`
  with body `{"delta": int, "reason": str}`.
- **Authorization** uses the existing chain: `merchant_id`, then membership, then
  product by merchant, then variant by product. Anything outside it returns 404.
  Session authentication and CSRF apply.
- **Concurrency.** Inside `transaction.atomic()`, the endpoint locks the
  `Inventory` row with `select_for_update()` and computes the new quantity from
  the locked value. It rejects a result below `reserved` or above the column
  maximum with 409, updates the stock, and inserts the audit row.
  - A delta, rather than "set quantity to X", is what makes concurrent changes
    safe.
  - The existing database check `quantity >= reserved` remains the final
    backstop.
- **Audit.** A dedicated `InventoryAdjustment` table records actor, variant,
  delta, quantity before and after, reason, and time.
  - Database checks enforce `delta <> 0` and `after = before + delta`.
  - Variant and actor use `PROTECT`, so history cannot be silently deleted.
  - I chose a typed table over JSON in `AuditLog` so the database enforces the
    record's accuracy.
- **Errors:** 400 for malformed input, 409 for a valid request the current stock
  cannot accept, 404 outside the merchant's scope.
- **Idempotency.** An optional `Idempotency-Key` header is stored on the
  adjustment, unique per operator.
  - A matching retry returns the original adjustment with `200` and
    `Idempotent-Replayed: true`, and stock changes once.
  - A key reused with a different request returns 409.
  - Same-key races are serialized by the row lock, and the unique constraint is
    the backstop.

## Correctness

The backend suite has 91 tests, run on MySQL inside Docker:

- **Model:** the database rejects a zero delta, inconsistent quantities, and a
  duplicate key. `PROTECT` blocks deleting a variant, product, or user that has
  adjustment history.
- **API, normal cases:** exact response and audit fields for increases and
  decreases, down to exactly `reserved`.
- **API, invalid input (400, nothing written):** zero, decimal, boolean, text,
  out of range, missing or blank or over-long reason, unexpected fields, bad
  `merchant_id`.
- **API, conflicts (409, nothing written):** below reserved, above the maximum,
  no inventory row.
- **API, denied:** another merchant's product, a variant under the wrong product,
  a non-member, unauthenticated, and a missing CSRF token.
- **Atomicity:** if the audit insert fails, the stock change rolls back.
- **Idempotency:** replay, mismatch, keys scoped per operator, invalid keys,
  retry after a failed attempt, and a simulated unique-key race.
- **Concurrency:** `TransactionTestCase` with real threads, each on its own MySQL
  connection.
  - 10 concurrent +1s are all applied, in a gap-free before/after chain.
  - Of 5 concurrent −2s against reserved stock, exactly 3 succeed.
  - 5 concurrent identical requests with one key apply once.
  - With the row lock removed, the first two tests fail: final stock was 7
    instead of 15.

An independent reviewer (a fresh agent without the builder's context) passed the
API with one non-blocking finding, listed below. That review predates a final
refactor that moved the helpers into `catalog/util.py` and `StockConflict` into
`catalog/exceptions.py`. The full suite was rerun after the refactor; the review
was not.

## Trade-offs, limitations, and next steps

- **The inventory adjustment UI (requirement 7) is not built.** The API is
  complete and tested. Next steps:
  - Add a form in the editor's Inventory tab.
  - Add `POST` support in `api.js`, with one `crypto.randomUUID()` key per
    submission, reused on retry.
  - Disable the submit button while a request is in flight.
  - Announce success and errors through `role="status"` and `role="alert"`.
- **F-01 (open, P2): idempotency keys match regardless of case.** The column uses
  MySQL's case-insensitive collation, so `key-a` and `KEY-A` count as the same
  key. The fix is `db_collation="ascii_bin"` on `idempotency_key`, a migration,
  and a test.
- **Adjustments do not appear on the product activity page.** The dedicated
  table is the only audit record. AGENTS.md still says mutations write
  `AuditLog`, and should be updated to match.
- **Failed requests are not stored for idempotency,** so a retry after a 409 is
  evaluated again. That is correct after stock changes, but it differs from APIs
  that also cache errors.
- **Remaining risks:**
  - A same-key race across different variants is tested only through a
    simulation.
  - A rare three-way same-key race could surface a MySQL deadlock as a 500.
  - The concurrency tests depend on timing.
- **Next:**
  - Seed audit data and measure whether the activity page needs a
    `(product, created_at, id)` index.
  - Expose an adjustment history endpoint.
  - Re-run the independent review on the final code.
