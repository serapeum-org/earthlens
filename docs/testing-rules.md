# Testing rules

Ten rules a reviewer can apply without reading the audit they came from. Each one exists because the suite
broke that way at least once; the enforcement column says what catches a violation now, so none of them
depends on someone remembering.

| # | Rule | Enforced by |
|---|------|-------------|
| `R1` | A live test's unavailability skip **must** carry the guard prefix — via `@pytest.mark.requires_host(...)`, `require_reachable` or `skip_on_upstream`, never a bare `pytest.skip` or a reachability `skipif` | `TestAvailabilitySkipsCarryTheGuardPrefix` |
| `R2` | **No network at module scope** in a test file. A probe goes behind `requires_host` or an autouse fixture | `TestNoImportTimeNetworkProbes` + `--disable-socket` |
| `R3` | A non-`e2e` test may not open a socket. Fake the transport, replay a cassette, use an emulator on loopback, or mark it `e2e` | `--disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1` |
| `R4` | A missing optional SDK is a **skip** (`pytest.importorskip`) or irrelevant (a `sys.modules` fake) — never a collection error | `TestOptionalSdkAbsenceIsASkip` |
| `R5` | Every marker is declared **once**, in `earthlens.testing._markers`, and changes what runs. A marker nothing selects on gets deleted | `--strict-markers` |
| `R6` | A backend's selector marker comes from the directory auto-tagger, not a hand-written `pytestmark` | `tag_items_by_directory` |
| `R7` | A cross-backend contract is a `Base*` class in `earthlens.testing`, subclassed by the provider. **Core's tests never import a provider** | `TestCoreTestsDoNotDependOnProviders` |
| `R8` | A new JSON-REST backend gets a cassette, not a new `FakeResponse` class; a new bucket backend gets the `s3_emulator` | `TestBucketTestsUseTheEmulator` (buckets); review (REST) |
| `R9` | Every e2e CI lane carries `--junitxml` + `assert_tests_ran.py`, `-rs`, a `timeout-minutes` and a path scope. An exemption is declared in the lane table | the lane table in `tests-e2e-reusable.yml` |
| `R10` | A chronically unreliable live backend goes to `quarantined` — not deleted, and not left reddening a shared lane | review |

## Why each one

**`R1` — the prefix is the whole design.** A skip whose reason starts with `live e2e skipped — ` means *upstream
was down*; any other skip means *this backend was never configured*. Both masked-lane guards read that
distinction, so a skip without the prefix makes an outage look like a missing credential — and, beside passing
neighbours, reddens the whole lane with advice to supply a credential that does not exist.

**`R2` — `-m` deselects after collection.** Collection imports every module, so a probe at module scope runs in
the unit lane too. Decorator expressions count: `@pytest.mark.skipif(not _probe(), ...)` is evaluated at
import.

**`R3` — measured caveat.** `pytest-socket` installs its block *per test*, so it does not see a socket opened
during collection. The flag is the regression net behind `R2`, not a substitute for it.

**`R4` — `extras: all` hides this.** Every CI lane installs every SDK, so "the extra is missing" is a state the
matrix cannot reach — but it is the state every `pip install earthlens` starts in. The `no-extras` lane exists
to run that case.

**`R5` — a warning is not an error.** An undeclared marker degrades to `PytestUnknownMarkWarning`, and a marker
that warns selects nothing. In a lane keyed on `-m "e2e and <backend>"`, that is a silently empty run.

**`R7` — core is released on its own.** A core test that imports a provider has an undeclared dependency on a
separately versioned distribution. The `Base*` classes let the provider assert the contract from its own lane,
where the failure is attributed correctly.

**`R8` — the tier exists, and the bucket half is enforced.** `earthlens.testing` ships `s3_emulator` (an
in-process S3) and `vcr_config` (cassettes, record mode `none`, credentials filtered). All six bucket backends
— goes, nwm, nwp, dem, radar, s3 — now run against the emulator, and `TestBucketTestsUseTheEmulator` fails any
new class that re-implements the S3 listing contract. Four cases are allow-listed, each because a real bucket
cannot express them: two drive a second page of pagination (the emulator paginates only past a thousand keys),
one asserts what the SDK was *called with*, and one injects a mid-transfer failure.

The REST half is still review-only: a cassette has to be recorded against the live service, so it is captured
when that backend is next touched with credentials to hand, not retrofitted in bulk.

## The three test tiers

| Tier | What it is | How to write one |
|------|-----------|------------------|
| 1. no network | the default. Fakes on the `requests` seam | `fake_http` / `FakeResponse` from `earthlens.testing` |
| 2. offline, real wire | a recorded exchange or an in-process service | `@pytest.mark.vcr` (cassette) or the `s3_emulator` fixture |
| 3. live | the real provider, credentials and all | `@pytest.mark.e2e` plus `@pytest.mark.requires_host(...)` |

Tier 2 is the one to reach for when a hand-written double would have to guess at the provider's behaviour —
the error body, the pagination envelope, the exact `ClientError` code. Those guesses are where the doubles
drifted from reality.
