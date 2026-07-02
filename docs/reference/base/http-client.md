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

http = HttpClient(headers={"X-API-Key": api_key}, timeout=60.0)

# JSON GET with automatic 429/5xx retry + Retry-After honouring:
payload = http.get_json("https://api.example.org/v1/things", params={"bbox": "1,2,3,4"})

# Streamed download to disk with a tqdm bar (sized from Content-Length):
http.download("https://example.org/big.tif", dest, progress=True)
```

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
