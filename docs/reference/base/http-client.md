# HTTP client (`earthlens.base.http`)

`HttpClient` is the shared `requests`-based transport for the REST-style backends. It owns the chores every
backend used to hand-roll — a pooled session, a sensible `User-Agent`, a per-request timeout, a
`Retry-After`-aware `429`/`5xx` back-off loop, JSON decoding, and a streamed download with a progress bar — so a
new backend keeps only the `API`-shaped parts: endpoint paths, query params, pagination, auth-header values, and
response parsing.

> **Don't hand-roll a session or a retry loop.** If you find yourself writing `requests.Session()`, a
> `while` loop that inspects `429` / `Retry-After`, or an `iter_content` chunk loop, reach for `HttpClient`
> instead. Backends that pre-date it are being migrated onto it.

## Consuming it

Construct one client per backend with the default headers it needs, then call the verbs:

```python
from earthlens.base.http import HttpClient

http = HttpClient(headers={"X-API-Key": api_key})

# JSON GET with automatic status + transport retry and Retry-After honouring:
payload = http.get_json("https://api.example.org/v1/things", params={"bbox": "1,2,3,4"})

# Streamed download to disk with a tqdm bar (sized from Content-Length):
http.download("https://example.org/big.tif", dest, progress=True)
```

## Retry policy

Two kinds of failure are retried, on separate budgets, because they warrant opposite policies.

**Status retries** cover `429` / `500` / `502` / `503` / `504` and honour `Retry-After`. A `413` / `429` / `503`
is the server asking for a later attempt, so it is replayed for any method. A `500` / `502` / `504` means the
server already had the request and may have acted on it, so those are replayed only for idempotent methods.

**Transport retries** cover the failures that never reach a status line — a refused or reset connection, a DNS
blip, a read timeout, a body truncated mid-stream. These are on by default; they were opt-in before, which meant
a TCP reset partway through a large granule threw away the whole transfer.

The two phases have their own budgets:

| Failure | Budget | Replayed for a `POST`? |
|---|---|---|
| **connect** — refused, unresolvable, connect timeout | `connect_retries` (1) | yes — the request never reached the server |
| **read** — reset mid-response, read timeout, truncated body | `read_retries` (= `max_retries`) | no, unless `retry_unsafe_methods=True` |
| `SSLError`, `ProxyError` | never retried *under the default set* | — |

A backend that already wraps its calls in its own retry or resilience loop now has two layers: the client retries
the transport, and the backend retries the call. The budgets multiply rather than add, so an outer loop of 3 over a
client of 5 is up to 18 attempts. If that is not what you want, pass `retry_on_exceptions=()` to opt the client's
transport retry out and keep the outer loop as the single authority.

If your endpoint is a `POST` that is safe to replay — a search or query API, an idempotent RPC — pass
`retry_unsafe_methods=True`. Without it neither a transport failure nor a `5xx` is replayed for that verb, and
the suppression is logged at debug level rather than being silent.

The connect budget is small on purpose: a host that refuses a connection rarely starts accepting one within a
back-off window, so a generous budget only turns a clear failure into a slow one.

`timeout` is a `(connect, read)` pair by default — `(10.0, 60.0)` — so a dead host fails in ten seconds while a
slow transfer keeps a full read budget. A bare float still works and applies to both phases.

The default `User-Agent` is `earthlens/{version}` — deliberately **non-Mozilla**, because the DIGITAL.CSIC
Anubis anti-bot wall (SPEIbase) blocks browser-like agents. Pass `user_agent=` for a descriptive contact string
(e.g. Overpass / ohsome etiquette).

## Testability

Both the transport and the wait are injectable, so the whole client is unit-testable with a fake session and no
real delays:

```python
client = HttpClient(session=fake_session, sleep=captured_waits.append)
```

Pagination and response-envelope parsing stay in each backend — the client owns only *how bytes move*, never
the `API` shape.

## API

::: earthlens.base.http.HttpClient
