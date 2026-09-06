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

## Downloads read the whole object, and verify it

By default `download` reads the object **once, whole**, on every attempt: it generates no `Range` header of its
own, and a retry re-requests from byte 0 rather than appending to what is already on disk. Resuming is available
but opt-in — see [Resuming a single file](#resuming-a-single-file-resumetrue) below.

Restarting is the default because appending to a partial file is only safe if the new bytes provably belong to
the same representation as the old ones, and most servers do not give you enough to prove it: `Accept-Ranges: bytes` is advertised by hosts that then ignore `Range` and send the
whole body from zero; `Last-Modified` has one-second resolution, so a validator can match across a real change;
and a server may answer `416` without the `Content-Range` that would say how much it actually has. Every one of
those produces a file that is the right *size* and the wrong *bytes* — corruption that survives to the user
rather than failing loudly. The opt-in path below refuses to resume unless the server clears every one of those
hazards.

What replaces it is verification after the fact:

| Response | `download` does |
|---|---|
| a body matching its `Content-Length` | publishes it |
| a body **short** of it | raises `IncompleteDownloadError`, retrying from the start until the read budget or a repeated byte count stops it |
| a body **longer** than it, or the same short count twice | raises `IncompleteDownloadError` without retrying — both repeat |
| no usable length (chunked, `Content-Encoding`, contradictory duplicates) | publishes it unchecked; there is no claim to check |
| a `206` to a request that carried no `Range` | raises `UnsolicitedPartialContentError` without retrying |
| a break *after* the last byte, when the size already matches | keeps it; the equality is the whole proof |

The table above is what `verify_length=True` (the default) buys. Pass `verify_length=False` for a server that
misreports the size of a body it generates on the fly, where the check would fail a download that is actually
fine. It distrusts the advertised length in **both** directions, so an over-long body is published too, and it
disables the salvage, whose proof is the same size equality. It cannot be combined with `resume=True`, which is
addressed by that same length — the pair raises `ValueError`.

Because the check compares against bytes as delivered, `download` sends `Accept-Encoding: identity` — but only
when neither the call nor the constructor named that header in any casing, so a backend that needs `gzip`, or
that sets `identity` itself to protect a magic check, keeps what it asked for.

A `Range` you pass yourself is honoured verbatim: the response is accepted at its own `Content-Length`, and
`download` does not check that the server returned the range you asked for.

Interrupted *multi-file* jobs resume at file granularity — `_is_complete()` skips the granules already on disk
(see [contracts](contracts.md)).

## Resuming a single file (`resume=True`)

Within one file, `download(url, dest, resume=True)` will continue a broken transfer instead of re-reading it. It
is **off by default**: everything above is what the other callers get.

Resume is only *attempted* when the first response proved it is possible — a `200` advertising
`Accept-Ranges: bytes`, a known `Content-Length`, no content coding, and a **strong** `ETag`. A weak tag
(`W/"..."`) never arms, and `Last-Modified` is never used: its one-second resolution matches across a change made
inside the same second.

Arming is the cheap half. A strong `ETag` is **not** sufficient — measured, `data.worldpop.org` sends one and then
answers a `Range` with `200` and the whole body. So the binding checks are on the reply, and all must hold before
one byte is appended:

| check | why |
|---|---|
| status is `206` | absorbs the `200`-with-the-whole-object case, a `416`, a redirect, an error status |
| `Content-Range` start, end and total all match what was asked | the object has not been replaced or re-cut |
| `ETag` still names the anchored representation | a `206` may not rename what `If-Range` selected |
| no content or transfer coding | `Range` addresses the *encoded* octets; the staged count is of decoded ones |
| the first 64 KiB re-send a window already on disk and **match it byte for byte** | see below |

That last one is the only check that reads the body. Every other gate compares one server claim against another,
so a server deriving a truthful `Content-Range` from the request while streaming a different slice satisfies all
of them. Re-reading a window we already hold is what makes the append safe.

Anything else discards the staged bytes and re-reads the whole object, and resume then stays off for the rest of
the call — so a badly-behaved server costs exactly one extra request, not a restart/resume cycle. Only bytes
*this call* wrote are ever built on, so a `.part` left by a killed run is never mistaken for a prefix, and the
assembled file passes the same length and `expect_magic` gates as a whole-object read.

Measured: geofabrik, DWD radklim (13.5 GB) and GHSL all emit strong ETags and honour `Range` exactly; worldpop
and Zenodo do not arm or are refused.

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
