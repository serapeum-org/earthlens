# EEA (`eea_aq`) — install & access

The EEA download service is **public** — there are no credentials, no
API key, and no login. The only prerequisite is the optional `airbase`
dependency, which this backend imports lazily.

## 1. Install the `[eea_aq]` extra

```bash
pip install earthlens[eea_aq]
```

This pulls [`airbase`](https://pypi.org/project/airbase/) (≥ 1.0), the
MIT-licensed client for the EEA download service. The package imports
without it — the SDK is only imported the first time you call
`download()` — so `import earthlens` and every other backend keep
working when the extra is absent. If you construct the EEA backend
without the extra installed, the first `download()` raises a clear
`ImportError` naming the extra.

## 2. No credentials

There is nothing to authenticate. The EEA service serves the data
anonymously over HTTPS; `airbase` requests per-country Parquet URLs and
downloads them directly.

## 3. Running in Jupyter

`airbase` performs its download with `asyncio.run()` internally, which
normally clashes with a notebook kernel's already-running event loop. The
backend handles this transparently: when it detects a running loop it
applies [`nest_asyncio`](https://pypi.org/project/nest-asyncio/) (present
in any Jupyter install via `ipykernel`) so the nested run succeeds — no
code change is needed in your notebook. In the rare case you run inside a
custom event loop without `nest_asyncio` installed, the backend raises a
clear `RuntimeError` naming the fix (`pip install nest_asyncio`).

## 4. What to expect on volume

Because the service is queried per country and per reporting era, a
whole-country / multi-year request can download a lot of Parquet. Keep
requests bounded:

- Prefer an **explicit `country=`** (a small country such as Malta
  `"MT"` is ideal for a first run) over a bbox that sweeps several large
  countries.
- Keep the **year range tight** — each extra era (`Historical` /
  `Verified` / `Unverified`) is a separate download.

The gated live e2e test (`tests/eea_aq/test_eea_e2e.py`) is marked `eea`
and downloads one small country + pollutant + year.
