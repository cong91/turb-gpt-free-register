# Gmail API URL voi shop.qan8.com

QAN8 la purchase adapter noi bo, dung QAN8 simple Open API tai
<https://shop.qan8.com/api-docs> de mua source Gmail API URL khi can. Runtime
chi co mot nguon dang ky la `gmail_api_url`; source nhap thu cong va source
mua tu QAN8 deu vao cung canonical ledger. QAN8 khong co inventory, lane hay
alias assignment rieng.

## Configuration

Set the canonical registration source and the purchase adapter credentials in
`.env` or through the authenticated WebUI configuration editor:

```dotenv
EMAIL_SOURCE=gmail_api_url
QAN8_API_BASE=https://shop.qan8.com
QAN8_API_KEY=your_qan8_api_key
QAN8_GMAIL_SKU_ID=your_gmail_sku_id
QAN8_REQUEST_TIMEOUT=15
QAN8_ORDER_TIMEOUT=120
```

The API key is never copied into job context, logs, responses, or exports. The
SKU is configured rather than hard-coded because product availability can
change. The documented endpoints used by the adapter are:

- `GET /api/v1/open/products`
- `GET /api/v1/open/balance?api_key=...`
- `POST /api/v1/open/orders`
- `GET /api/v1/open/orders/{out_order_no}?api_key=...`

Every purchase request sends `quantity=1` and a persisted `out_order_no`.

## Count and lane model

One Gmail API URL source has a fixed capacity of 12 aliases. The alias count is
calculated by the runtime and is not a QAN8 configuration value.

WebUI `count` is the number of source groups to use or purchase. For example,
`count=3` creates 36 registration jobs and targets 3 source groups. Sub2API
`count` remains the number of account jobs: `count=30` creates 30 jobs and
needs `ceil(30 / 12)` source groups.

`requested_workers` is the physical executor width. The runtime creates at
most one logical Gmail lane per source group, then gives each lane its own
canonical lazy batch:

```text
required_source_groups = ceil(registration_jobs / 12)
effective_lanes = min(requested_workers, required_source_groups)
source_group_lane = source_group_index % effective_lanes
```

Every source group contributes up to 12 jobs, and all of its aliases share the
same exact `code_url`. A lane processes the 12 jobs for its current source
sequentially before it materializes or purchases its next source. Existing
imported aliases are reused before paid purchase. The physical thread count and
the source-group count are separate values.

## Lazy refill

For a WebUI request of 3 source groups (36 jobs), three workers, and 12 aliases
per source:

1. The runtime persists three empty canonical batches, one for each lane, and
   places no QAN8 order yet.
2. Each lane runner processes its own 12-job source group in sequence. No lane
   can claim an alias from another lane batch.
3. When a lane needs its first alias, it reuses an eligible imported source or
   materializes exactly one QAN8 source with `quantity=1`.
4. After all 12 aliases of that source are exhausted and the same lane still
   has jobs, its batch-scoped provision lease permits exactly one next source
   purchase.
5. For five source groups and three workers, the lane plans are 24, 24, and 12
   jobs: the first two lanes consume two sources each and the third consumes
   one. The three lane runners can proceed in parallel.

The canonical ledger enforces one active assignment per alias and one active
assignment per `code_url`. A `code_url` is owned by one canonical lane batch,
so a lane never falls back to aliases from an older or parallel batch. New
source groups link all aliases before they can be claimed. Existing databases
are migrated before new claims are accepted; an alias already consumed, failed,
or held by another worker cannot be claimed again. There is no separate QAN8
runtime source or QAN8 assignment table in the new flow.

## Order recovery and failure

Order intents, order numbers, source groups, aliases, leases, and assignments
are stored in the canonical `turb.sqlite3`. If the create response is unknown,
the intent becomes `unknown` and later attempts use order lookup only. The
adapter does not blindly POST another order with the same order number.

The delivery contract is one non-empty line in this form:

```text
source@gmail.com----https://provider.example/code
```

The parser accepts only Gmail addresses and HTTP(S) code URLs. It rejects
credential-bearing or otherwise ambiguous lines. A quantity-one order must
produce exactly one source record. Invalid delivery is recorded as
`delivery_unparsed` and does not create a registration assignment.

The Gmail API URL poller handles OTP response semantics, including stale-code
protection. A successful registration consumes the alias. An early
registration failure releases that alias when it is safe to reuse it. A
provider-level `code=602` failure fails every matching alias and retires every
source group sharing that `code_url`. For a source delivered by a QAN8 purchase,
the first request response is recorded. Only when that first response is `602`
(before any `601`, other response, or OTP response) is the `uid` query value
sent to `POST /api/after-sales/check` with `{"uid": "..."}`. Imported sources
and sources that already returned an OTP are quarantined locally but are never
sent to QAN8 after-sales.

Automated tests mock all QAN8 HTTP calls. Routine verification never creates a
live order or spends QAN8 balance.
