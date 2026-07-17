# Troubleshooting: pixi dependency resolution timeouts

`earthlens` has a large dependency surface — the `all` extra alone pulls in dozens of provider SDKs. On
some networks, `pixi update` / `pixi install` (and therefore `pixi lock`, which does the same PyPI
resolution) can fail or hang partway through resolving that graph, even though the network itself is
fine.

## Symptom

`pixi update`, `pixi install`, or `pixi lock` exits with an error like:

```text
Error:   × failed to solve the pypi requirements of environment 'dev' for platform 'win-64'
  ├─▶ failed to resolve pypi dependencies
  ├─▶ Failed to fetch: `https://pypi.org/simple/<some-package>/`
  ├─▶ Request failed after 3 retries
  ├─▶ error sending request for url (https://pypi.org/simple/<some-package>/)
  ╰─▶ operation timed out
```

or the command just hangs indefinitely with no progress.

!!! tip "Rule out real network downtime first"
    Before changing anything, confirm PyPI itself is reachable:
    ```bash
    curl -sS -o /dev/null -w "%{http_code} in %{time_total}s\n" https://pypi.org/simple/<some-package>/
    ```
    If that returns almost instantly (e.g. `200 in 0.1s`), the network is fine — the timeout is coming
    from pixi's own HTTP client, not the connection.

## Why this happens

pixi doesn't shell out to a separate `uv` binary — it vendors `uv`'s Rust crates internally for PyPI
dependency resolution. Those crates default to a **30-second per-request timeout** and give up after a
handful of retries. Resolving a large extras graph issues many PyPI metadata requests; if even one of
them is slow (a cold CDN edge, a rate-limited response, a large sdist), the whole resolve fails.

## Fix: raise the request timeout, lower concurrency

Two knobs help, and both are confirmed to work against this repository's dependency graph:

1. **`UV_HTTP_TIMEOUT`** — an environment variable that pixi's vendored `uv` client genuinely reads.
   Raise it well above the 30-second default (e.g. to 180 seconds).
2. **`--concurrent-downloads`** — a **pixi CLI flag** (default `50`) that caps how many requests are in
   flight at once. Lowering it reduces the chance of hitting a slow/throttled response. This is a flag,
   not an environment variable — `pixi update --help` shows no `[env: ...]` binding for it.

=== "bash"

    ```bash
    export UV_HTTP_TIMEOUT=180
    pixi update --concurrent-downloads 8
    ```

=== "PowerShell"

    ```powershell
    $env:UV_HTTP_TIMEOUT = "180"
    pixi update --concurrent-downloads 8
    ```

Scope to a single package and/or environment when you only need to bump one dependency:

```bash
pixi update cleopatra -e dev --concurrent-downloads 8
```

## When the lock file itself seems stuck

`pixi.lock` is committed to git, so it's always safe to delete and regenerate from scratch — if the
regeneration fails partway, `git checkout -- pixi.lock` restores the previous working lock.

```bash
git checkout -- pixi.lock   # confirm you're starting from a clean, working lock
rm pixi.lock
export UV_HTTP_TIMEOUT=180
pixi update --concurrent-downloads 8
```

Prefer `pixi update` over `pixi lock` for this repository — the two do equivalent PyPI resolution work,
but `pixi update` has been the reliable one in practice.

Once it succeeds, sanity-check the package(s) you care about actually landed at the expected version
before committing:

```bash
grep -oE "cleopatra-[0-9.]+" pixi.lock
```

## What doesn't work

There is **no working retry-count environment variable**. We looked for one directly — extracting all
`UV_`-prefixed and `PIXI_`-prefixed strings from the compiled `pixi` binary turns up no production retry
knob; the only retry-adjacent variable present (`UV_TEST_NO_HTTP_RETRY_DELAY`) is an internal test-only
switch inside `uv`'s own test suite, not something meant for tuning a real resolve. The retry count (three
attempts) appears to be hardcoded via the `reqwest-retry` crate pixi vendors. If a longer timeout still
isn't enough, the practical fallback is simply to re-run the command — `pixi.lock` only needs to succeed
once, and each attempt starts fresh.

## Verifying an environment actually installs

A successfully regenerated `pixi.lock` and a successful `pixi install` are two different things — the
second can still fail for unrelated reasons (e.g. a package with no prebuilt wheel for your Python version
needing to compile from source). If `pixi install` fails on a package you didn't touch, check whether it's
a pre-existing issue by comparing against another already-working environment for this project before
assuming your lock change is at fault.
