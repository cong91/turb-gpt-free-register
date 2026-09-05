# QAN8 Gmail API provider

This provider uses the QAN8 simple Open API documented at
<https://shop.qan8.com/api-docs> only to acquire Gmail API URL source mailboxes
on demand. The purchased source and its aliases are registered in the same
`gmail_api_url` item ledger used by manually imported Gmail API batches; QAN8
does not own a second alias inventory.

## Configuration

Set the provider and credentials in `.env` or through the authenticated WebUI
configuration editor:

```dotenv
EMAIL_SOURCE=qan8_gmail_api
QAN8_API_BASE=https://shop.qan8.com
QAN8_API_KEY=your_qan8_api_key
QAN8_GMAIL_SKU_ID=your_gmail_sku_id
QAN8_REQUEST_TIMEOUT=15
QAN8_ORDER_TIMEOUT=120
QAN8_ALIASES_PER_SOURCE=12
```

The API key is never copied into job context, logs, responses, or exports.
The SKU is configured rather than hard-coded because product availability can
change. The documented endpoints used by the client are:

- `GET /api/v1/open/products`
- `GET /api/v1/open/balance?api_key=...`
- `POST /api/v1/open/orders`
- `GET /api/v1/open/orders/{out_order_no}?api_key=...`

An order request always sends `quantity=1` and a persisted `out_order_no`.

## Worker and lane model

The registration count is the number of jobs to process. `requested_workers`
is the physical executor width and the maximum number of provider lanes that
may claim concurrently. `aliases_per_source` is the reusable capacity of one
purchased source; it never changes the requested job count:

```text
effective_lanes = min(requested_workers, registration_count)
lane_id = job_position % effective_lanes
```

Each lane owns one active source mailbox and processes that source's aliases
FIFO, with at most one active assignment in the lane. Physical
`ThreadPoolExecutor` thread names are not ownership identifiers.

For a request with `count=6`, `workers=3`, and `aliases_per_source=12`, the
service creates six jobs and one provider lane:
`min(3, ceil(6 / 12)) = 1`. The physical executor may still have three worker
threads, but they queue on the same canonical source/code URL; a completed or
released assignment lets the next job claim the next alias. Existing Gmail API
inventory is reused before any paid purchase. A lane keeps its source while
aliases remain available; only when all aliases for that source are exhausted
and pending jobs remain does it buy one replacement source. WebUI and
automation both keep `count` as the number of registrations; alias capacity
never multiplies the job count. The client generates the configured aliases
locally; aliases are not purchased from QAN8. Every alias in a source group
shares that source's exact `code_url` and therefore receives OTP through the
same inbox.

## Lazy refill

For a batch of 36 jobs, three workers, and 12 aliases per source:

1. The batch is persisted without any QAN8 order.
2. Three provider lanes are opened (`min(3, ceil(36 / 12))`), but no order is
   placed until a lane needs an alias and the canonical Gmail inventory is
   empty.
3. Each lane consumes its canonical aliases in order; a worker waits when the
   shared code URL is already being polled, then claims the next alias after the
   previous assignment is completed, released, or failed.
4. After all aliases in a source are exhausted and pending jobs remain, that
   lane purchases one replacement with `quantity=1`.
5. Other lanes keep their current source. Lifetime orders may exceed the
   initial source count, while active source ownership remains one source per
   lane.

The database enforces active uniqueness for both `source_email` and
`code_url`, and one active assignment per lane. Every QAN8 alias points to the
canonical Gmail API batch item, so Gmail API workers and QAN8 workers contend
on the same active code URL and the same alias state. New source groups link
all aliases before they can be claimed. Existing databases are migrated as one
SQLite transaction before new claims are accepted; an alias already consumed,
failed, or held by the other provider cannot be claimed again. There is no
global source queue and no fallback to Outlook or another provider after an
explicit QAN8 failure.

## Order recovery and failure

Order intents, order numbers, source groups, aliases, leases, and assignments
are stored in the canonical `turb.sqlite3`. If the create response is unknown, the
intent becomes `unknown` and later attempts use order lookup only. The client
does not blindly POST another order with the same lane.

The initial delivery contract is one or more non-empty lines in this form:

```text
source@gmail.com----https://provider.example/code
```

The parser accepts only Gmail addresses and HTTP(S) code URLs. It rejects
credential-bearing or otherwise ambiguous lines. A quantity-one order must
produce exactly one source record. Invalid delivery is recorded as
`delivery_unparsed` and does not create a registration assignment.

The existing Gmail API URL poller handles OTP response semantics, including
stale-code protection. A successful registration consumes the alias. An early
registration failure releases that alias when it is safe to reuse it. A
provider-level `code=602` failure fails every matching alias and retires every
source group sharing that `code_url`; the order remains visible for operator
review and is not automatically refunded.

Automated tests mock all QAN8 HTTP calls. Routine verification never creates a
live order or spends QAN8 balance.
