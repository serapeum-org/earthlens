# Contributing

How to set up a development environment, run the tests, and add a new provider backend. For the code of conduct
and the pull-request process, see [`CONTRIBUTING.md`](https://github.com/serapeum-org/earthlens/blob/main/CONTRIBUTING.md)
in the repository root.

## Development setup

earthlens is a [uv](https://docs.astral.sh/uv/) **workspace**. The root `pyproject.toml` declares
`[tool.uv.workspace] members = ["libs/core", "libs/providers/*"]`, and the seven distributions —
`earthlens-core`, the five thematic providers, and the `earthlens` meta-package — are its members.

```bash
git clone https://github.com/serapeum-org/earthlens.git
cd earthlens
uv sync --extra all --group dev
```

That installs core plus the five providers as editable, every backend SDK, and the dev tools.

!!! warning "Use `--extra all`, not `--all-extras`"

    `--all-extras` activates *every* extra, including both `argo` and `openeo`, which are mutually exclusive:
    `argopy` needs `xarray>=2025.7` while `openeo` needs `xarray<2025.1.2`. uv rejects the combination. The
    curated `all` extra includes `openeo` and `osm`, and omits only `argo` and `osm-pbf`, so it resolves.

    To work on the argo or osm side instead, prune the other:

    ```bash
    uv sync --all-extras --no-extra openeo --no-extra osm-pbf
    ```

Refresh the lockfile with `uv lock`; `uv lock --check` verifies it is current (the CI `--locked` gate).

## Running the tests

```bash
# unit + integration with coverage (e2e is opt-in)
uv run pytest -m "not e2e" --cov=libs --cov-report=xml

# one file, one test
uv run pytest libs/core/tests/test_earthlens.py -v
uv run pytest libs/core/tests/test_earthlens.py::test_name -v
```

Tests are **co-located with each distribution**, not in a repo-root `tests/`. `testpaths` lists the six roots:
`libs/core/tests` plus `libs/providers/<theme>/tests` for each of the five themes.

End-to-end tests carry the `e2e` marker, perform live network downloads, and are skipped unless you select them
with `-m e2e`.

Test categories are selected by **pytest markers only** — never by environment variables. Every backend has its
own marker (`chc`, `gee`, `cmems`, …) alongside the cross-cutting ones (`slow`, `fast`, `unit`, `integration`,
`e2e`). Run `uv run pytest --markers` for the full list. Credentials a live test needs may come from the
environment, but whether a test *runs* is decided by its marker and `-m`.

## Linting

```bash
pre-commit run --all-files
```

The stack is **ruff** (`ruff-check` + `ruff-format`) with mypy and bandit, plus shellcheck and beautysh for shell
scripts. CI runs `lint.yml` and `pip-audit.yml` on every push.

## Adding a provider backend

Every backend under `libs/providers/<theme>/src/earthlens/<pkg>/` follows the same layout, so all 48 read the same
way. Match it exactly.

```
libs/providers/<theme>/src/earthlens/<pkg>/
  __init__.py     # module docstring (required) + __all__
  backend.py      # the AbstractDataSource subclass — ALWAYS backend.py, never <pkg>.py
  catalog.py      # the catalog loader
  catalog/        # OR <pkg>_data_catalog.yaml — the catalog data
  auth.py         # when the provider needs credentials
  _helpers.py     # private, stateless helpers (optional)
```

The steps:

1. **Pick the theme** — `atmosphere`, `ocean`, `imagery`, `land`, or `hazards`. That decides which distribution
   ships it.
2. **Subclass `AbstractDataSource`** in `backend.py`. Only `download()` and `_check_input_dates()` are abstract;
   the other hooks (`_initialize`, `_create_grid`, `_api`) have working defaults.
3. **Declare `OUTPUT_KIND`** — `raster`, `vector`, `tabular`, or `mixed`. It governs what `download()` returns
   and whether `aggregate=` is accepted. See [Architecture](overview/architecture.md).
4. **Write the catalog** following the storage rule: a sharded `catalog/` directory for a large or multi-family
   catalog, a single `<pkg>_data_catalog.yaml` for a small single-family enumeration.
5. **Register the entry point** in the theme distribution's `BACKENDS` table, so `discover_backends()` finds it.
   Core must not name your backend.
6. **Declare dependencies.** Anything imported eagerly goes in core `[project.dependencies]`; only a lazily
   imported per-backend SDK may live in a `[<backend>]` extra — and it must still be declared. Then run
   `uv lock`.
7. **Add a pytest marker** for the backend in the root `pyproject.toml` and tag its tests.
8. **Add docs** — a `docs/reference/<pkg>/` folder (`introduction.md`, `usage.md`, and where relevant
   `authentication.md` / `datasets.md`), a row in
   [Supported providers](reference/providers.md), an entry on the [landing page](index.md), example notebooks
   under `docs/examples/<pkg>/`, and nav entries in `mkdocs.yml`.

### What does not belong here

Generic GIS functionality — reprojection, resampling, mosaicking, clipping, CRS utilities, format I/O for
GeoTIFF / NetCDF / Zarr / COG, raster↔vector conversion, zonal statistics, generic plotting — belongs in
[pyramids](https://github.com/serapeum-org/pyramids), the GIS backend earthlens consumes. earthlens owns
**fetching**: per-provider SDKs and protocols, catalogs, request shaping, authentication, retry policy, and
download orchestration.

If you find yourself writing a CRS helper or a GeoTIFF writer under `libs/*/src/earthlens/`, put it in pyramids
and import it instead.

## Code conventions

- **`from __future__ import annotations`** at the top of every `.py` file, below the module docstring and above
  every other import.
- **Modern typing** — `list[int]`, `dict[str, T]`, `X | None`. Never `List`, `Dict`, `Optional`, `Union`.
- **PEP 8 casing** — `snake_case` for functions/variables/modules, `PascalCase` for classes, `UPPER_SNAKE_CASE`
  for module constants.
- **Google-style docstrings**, rendered by mkdocstrings as Markdown. Use *single* backticks for inline code —
  double backticks are a reStructuredText holdover and are not idiomatic here.

## Documentation

The site is mkdocs-material with mkdocstrings; `mkdocs.yml` holds the nav.

```bash
uv run mkdocs serve          # live preview
uv run mkdocs build --strict # what CI runs: warnings become errors
```

Notebook examples live under `docs/examples/<pkg>/`. Because a notebook's kernel runs with its **own directory**
as the working directory, paths inside a notebook must be relative to the notebook itself — a notebook at
`docs/examples/<pkg>/nb.ipynb` reaches `examples/data/` via `../../../examples/data`.

**Notebook outputs are stripped on commit.** The `nbstripout` pre-commit hook clears outputs and execution
counts from every notebook under `docs/examples/`, with one exception: `docs/examples/showcases/`, whose
notebooks exist to show a rendered animation or plot and need credentials the docs build does not have, so
nothing could regenerate what was stripped.

The practical consequence is that the docs build does **not** execute notebooks, so a stripped notebook
publishes as code with no results. That is the current trade-off: the repository stays small, and the example
pages show the call rather than its output. If you are adding a notebook whose result is the point, put it under
`showcases/` so its outputs survive.

Keep those outputs small. An embedded base64 image or video is counted in full on every clone, so prefer a
linked asset, and reach for an embedded one only when the page is meaningless without it. The two 2026
showcase notebooks sit at roughly 1.5–1.8 MB each and are the practical ceiling — treat them as the limit,
not the example to follow.
