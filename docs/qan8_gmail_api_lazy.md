# QAN8 Gmail API lazy provider

This provider uses the QAN8 simple Open API documented at
<https://shop.qan8.com/api-docs> to acquire Gmail API URL source mailboxes on
demand. It is separate from the manually imported `gmail_api_url` pool.

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

The registration count is the number of aliases to process. The effective
worker count is the number of logical lanes, after the existing worker clamp:

```text
effective_workers = min(requested_workers, registration_count)
lane_id = job_position % effective_workers
```

Each lane owns one active source mailbox and processes that source's aliases
FIFO, with at most one active assignment in the lane. Physical
`ThreadPoolExecutor` thread names are not ownership identifiers.

For example, `workers=5` creates five lanes. The first request in each lane
places one independent QAN8 order, so the active topology is five different
source mailboxes. The client then generates the configured aliases locally;
aliases are not purchased from QAN8. Every alias in a source group shares that
source's exact `code_url` and therefore receives OTP through the same inbox.

## Lazy refill

For a batch of 36 jobs, three workers, and 12 aliases per source:

1. The batch is persisted without any QAN8 order.
2. Lanes 0, 1, and 2 each purchase one source only when their first job asks
   for an alias.
3. Each lane consumes its 12 aliases in order.
4. After a lane has no available or active alias left, that lane purchases one
   replacement with `quantity=1`.
5. The other lanes keep their current source. Lifetime orders may exceed the
   worker count, while active source ownership remains one source per lane.

The database enforces active uniqueness for both `source_email` and
`code_url`, and one active assignment per lane. There is no global source
queue and no fallback to Outlook or another provider after an explicit QAN8
failure.

## Order recovery and failure

Order intents, order numbers, source groups, aliases, leases, and assignments
are stored in `app_state.sqlite3`. If the create response is unknown, the
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
provider-level `code=602` failure fails the assignment and retires its source
group; the order remains visible for operator review and is not automatically
refunded.

Automated tests mock all QAN8 HTTP calls. Routine verification never creates a
live order or spends QAN8 balance.
