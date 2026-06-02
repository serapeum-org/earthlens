# GHSL — Authentication

**There is no authentication.** The JRC Global Human Settlement Layer is open
data served over anonymous HTTPS from the JRC JEODPP file tree
(`https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/`). No account, API
key, token, environment variable, or credentials file is required.

```python
from earthlens import EarthLens

# No credentials, no setup — this just works:
EarthLens(
    data_source="ghsl",
    variables=["GHS_POP"],
    start="2020-01-01", end="2020-12-31",
    lat_lim=[40.3, 40.5], lon_lim=[-3.8, -3.6],
    path="out/",
).download()
```

## The no-op auth class

For conformance with the package's `AbstractAuth` shape (every backend has an
`auth` module), GHSL ships a **no-op** `GhslAuth`:

* `GhslAuth().is_authenticated()` is always `True`.
* `GhslAuth().configure()` does nothing.
* `GhslCredentials` is an empty, frozen value object.

You never need to touch these — the backend wires them internally.

## Licence obligation (attribution)

The only obligation is **attribution**. GHSL is released under a CC-BY-style
licence: "reuse is authorised provided the source is acknowledged". The
required citation is available programmatically:

```python
from earthlens.ghsl import GHSL_ATTRIBUTION
print(GHSL_ATTRIBUTION)
# Source: European Commission, Joint Research Centre (JRC) — Global Human
# Settlement Layer (GHSL). Data available at https://ghsl.jrc.ec.europa.eu/.
```

Acknowledge the JRC GHSL when you publish maps, figures, or derived products.

## Rate limits / throttling

The JRC tree imposes no documented per-user rate limit, but it is a shared
public service. The backend streams downloads with bounded retries and
exponential backoff, and **caches** every downloaded `.tif` (a re-run with the
same request skips the network entirely). Keep AOIs modest — a fine-resolution
(100 m / 10 m) request over a large area fetches many ~5–6 MB tiles, and the
coarse whole-globe files are ~300 MB each.
