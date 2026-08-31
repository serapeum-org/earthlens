# JAXA — usage

## Quick start: AW3D30 elevation tile (authless)

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="jaxa",
    variables=["aw3d30"],        # alias for ALOS PRISM AW3D30 v3.2
    start="2000-01-01",          # AW3D30 is a static product; pass any window
    end="2030-12-31",
    lat_lim=[35.35, 35.40],      # ~Mt. Fuji
    lon_lim=[138.70, 138.78],
    resolution=1000.0,           # pixels per degree (~110 m); SDK snaps to native
    path="./out/jaxa",
)
written = lens.download()
print(written)  # [PosixPath('out/jaxa/aw3d30_DSM.tif')]
```

The output GeoTIFF is north-up, EPSG:4326, with one band per request
(default band from the catalog, or one file per `bands=` entry).

## Quick start: ALOS-2 PALSAR-2 product (credentialed SFTP)

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="jaxa",
    variables=["alos2-palsar2-l1-1"],
    start="2024-01-01",
    end="2024-01-02",
    lat_lim=[35.0, 36.0],
    lon_lim=[138.0, 139.0],
    path="./out/jaxa-palsar2",
    # Either set env vars instead:
    gportal_username="...",
    gportal_password="...",
)
written = lens.download()  # SFTP fetch via the gportal SDK
```

Search is anonymous but `download()` needs credentials. Empty time / AOI
windows return `[]` rather than raising.

## Request shape

| Parameter | Meaning |
|---|---|
| `variables` | Catalog dataset key(s) — canonical or friendly alias. All keys must resolve to the **same** protocol; mixed-protocol calls are rejected. |
| `start` / `end` | Inclusive date window. For `jaxa-earth` static products like AW3D30, pass a wide window — the SDK picks the single available date. |
| `lat_lim` / `lon_lim` | WGS84 bbox `[min, max]`. |
| `resolution` | `jaxa-earth` only — pixels per degree (`ppu`). Ignored for `gportal`. |
| `bands` | `jaxa-earth` only — list of band names. Overrides the catalog's `default_band`. One COG per band. |
| `gportal_username` / `gportal_password` | Explicit G-Portal credentials. When omitted, env vars `GPORTAL_USERNAME` / `GPORTAL_PASSWORD` are read. |
| `path` | Output directory (created if missing). |

The `temporal_resolution=` / `cadence=` arguments are accepted (the
facade requires them) but only the date-window bounds drive each
protocol — neither SDK takes an interval cadence.

## Listing the catalog

```python
from earthlens.jaxa import Catalog
cat = Catalog()
print(len(cat))                            # 124
print(cat.by_protocol("jaxa-earth")[:5])   # 118 STAC collections
print(cat.by_protocol("gportal"))          # 6 G-Portal mission products
cat.get("elevation")                       # resolves "elevation" -> "aw3d30"
```

Unknown keys raise a `ValueError` with a did-you-mean hint.

## Why `aggregate=` is not supported (yet)

`download(aggregate=…)` raises `NotImplementedError`. Both branches emit
per-date files (`jaxa-earth` returns the per-date tensor for the
requested window; `gportal.download` writes one file per matched
product); the standard `earthlens.aggregate` reducer needs a per-window
*stack* shape that the two branches don't both produce today. This is
planning gap **G6** — to be unblocked once multi-time tensors flow
through the `jaxa-earth` branch.

## Refreshing the catalog from the live archive

The `earthlens` CLI ships a refresher that re-enumerates the live JAXA
collections + G-Portal datasets so a stale local YAML can be regenerated:

```bash
earthlens datasets refresh jaxa            # validates the bundled YAML
earthlens datasets refresh jaxa --write    # rewrites the bundled YAML
```

The first form prints a summary (collection count, drift vs the bundled
catalog) without touching disk. The second rewrites the
`available_datasets:` index in `src/earthlens/jaxa/catalog/_index.yaml`
with the freshly probed ids (the curated per-mission shards under the
same `catalog/` directory are not touched). The per-mission shards are
generated from scratch by a build script kept with the maintainer's
capture notes, not shipped in the package — regenerate them by re-running
`earthlens datasets refresh jaxa --write` and re-probing.

## When the live e2e tests run

The end-to-end tests are gated:

* `pytest -m "jaxa and e2e"` — runs the live `jaxa-earth` pull
  unconditionally (authless), and the live `gportal` search only when
  `$GPORTAL_USERNAME` + `$GPORTAL_PASSWORD` are set in the environment.
* The default `pytest -m "not e2e"` run skips both, so the suite stays
  fully offline.
