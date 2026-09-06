"""Config + curated-vocabulary catalog for the ISIMIP backend.

Hosts :class:`Catalog`, the pydantic-backed reader for the bundled
`isimip_data_catalog.yaml`. ISIMIP is addressed by a *facet set*
(`simulation_round`, `climate_forcing`, `climate_scenario`, `climate_variable`,
`time_step`, `product`), which the ISIMIP repository REST API resolves to the
matching per-decade NetCDF granules. Those granules are huge (~1-2 GB each), so
the backend never pulls them whole — it submits a server-side cutout job that
returns the granule cut to the requested bbox.

This catalog holds only the *config* (the API + files-API URLs, the product /
time-step enumerations) plus the *request vocabulary* — the rounds / forcings
(GCMs) / scenarios / variables the backend validates each request against,
raising a did-you-mean error on an unknown facet. The blocks cover the
ISIMIP3b / ISIMIP3a InputData facets; dataset resolution and the authoritative
per-dataset licence still run against the live API.

:data:`CATALOG_PATH` is the path to the bundled YAML;
:func:`clear_catalog_cache` empties the `(path, mtime)` parse cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.catalog_source import load_catalog
from earthlens.base.yaml_loader import CatalogParseCache, load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "isimip_data_catalog.yaml"

_CATALOG_CACHE: CatalogParseCache = CatalogParseCache()


def clear_catalog_cache() -> None:
    """Empty the module-level ISIMIP catalog parse cache."""
    _CATALOG_CACHE.clear()


class Variable(BaseModel):
    """One curated ISIMIP climate variable row (the `climate_variable` leaf).

    A frozen value object with descriptive metadata only — ISIMIP variables carry
    no request-shaping parameters; the facet set selects the dataset and the
    cutout job returns the granule. The variable keys form the validated request
    vocabulary, so a `climate_variable` outside this block raises a did-you-mean
    error rather than reaching the API.

    Attributes:
        units: ISIMIP / CMIP6 CMOR unit (`"kg m-2 s-1"`, `"K"`, `"1"`).
        long_name: Human-readable description used in docs and logs.

    Examples:
        - Build a variable row directly:
            ```python
            >>> from earthlens.isimip import Variable
            >>> v = Variable(units="K", long_name="Near-surface air temperature")
            >>> v.units
            'K'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    units: str = ""
    long_name: str = ""


class Forcing(BaseModel):
    """One curated ISIMIP climate-forcing (GCM / reanalysis) row.

    Attributes:
        institution: The modelling centre / provider of the forcing.
        round: The simulation round the forcing belongs to (`"ISIMIP3b"` for the
            CMIP6 GCMs, `"ISIMIP3a"` for the obs-based reanalyses).
        description: Human-readable summary.

    Examples:
        - Read a forcing's round:
            ```python
            >>> from earthlens.isimip import Catalog
            >>> Catalog().get_forcing("gfdl-esm4").round
            'ISIMIP3b'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    institution: str = ""
    round: str = ""
    description: str = ""


class Scenario(BaseModel):
    """One curated ISIMIP climate-scenario row.

    Attributes:
        round: The simulation round the scenario belongs to.
        description: Human-readable summary.

    Examples:
        - Read a scenario's round:
            ```python
            >>> from earthlens.isimip import Catalog
            >>> Catalog().get_scenario("ssp585").round
            'ISIMIP3b'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    round: str = ""
    description: str = ""


class Round(BaseModel):
    """One curated ISIMIP simulation-round row.

    Attributes:
        description: Human-readable summary of the round.
        default_license: A *documentation* default licence label. The
            authoritative per-dataset licence is read live from the API
            (`rights.short` / `terms_of_use`); this field only documents the
            common case for the round.

    Examples:
        - Read a round's documentation licence:
            ```python
            >>> from earthlens.isimip import Catalog
            >>> Catalog().get_round("ISIMIP3b").default_license
            'CC0 1.0 (InputData / W5E5); OutputData varies per dataset'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = ""
    default_license: str = ""


def _parse_catalog(files: list[Path]) -> dict[str, Any]:
    """Parse the ISIMIP catalog YAML into `Catalog` construction kwargs.

    Args:
        files: The contributing YAML files (ISIMIP ships a single file).

    Returns:
        dict[str, Any]: The validated construction kwargs. The payload is cached,
            not a built `Catalog`, so `load()` makes a fresh instance per call.
            The row objects inside it are shared frozen pydantic models — treat
            them as read-only.

    Raises:
        ValueError: If a required config key is missing or a row fails
            validation.
    """
    path = files[0]
    data = load_yaml_strict(path) or {}
    data_url = data.get("data_url")
    files_api_url = data.get("files_api_url")
    if not data_url or not files_api_url:
        raise ValueError(
            f"{path} is missing its 'data_url:' / 'files_api_url:'. The ISIMIP "
            "catalog must name the repository API and the files-API cutout endpoint."
        )
    variables = Catalog._parse_block(path, data.get("variables"), Variable)
    forcings = Catalog._parse_block(path, data.get("forcings"), Forcing)
    scenarios = Catalog._parse_block(path, data.get("scenarios"), Scenario)
    rounds = Catalog._parse_block(path, data.get("rounds"), Round)
    return {
        "data_url": data_url,
        "files_api_url": files_api_url,
        "products": list(data.get("products") or []),
        "time_steps": list(data.get("time_steps") or []),
        "datasets": variables,
        "forcings": forcings,
        "scenarios": scenarios,
        "rounds": rounds,
    }


class Catalog(AbstractCatalog[Variable]):
    """Config + curated-vocabulary catalog for the ISIMIP backend.

    Reads the bundled `isimip_data_catalog.yaml` (shipped as package data) and
    exposes its `variables:` block as a map of :class:`Variable` rows keyed by
    `climate_variable` under the inherited :attr:`datasets` field, plus parallel
    :attr:`forcings`, :attr:`scenarios`, and :attr:`rounds` maps and the API
    config (:attr:`data_url`, :attr:`files_api_url`, :attr:`products`,
    :attr:`time_steps`). Instantiate with no arguments (`Catalog()`);
    :func:`model_post_init` loads and validates the YAML in one pass and caches
    it by `(path, mtime)`.

    Attributes:
        data_url: The ISIMIP repository REST API base (datasets / files search).
        files_api_url: The ISIMIP files API v2 (the async cutout job endpoint).
        products: The request products (`InputData` / `OutputData`).
        time_steps: The temporal resolutions (`daily` / `monthly`).
        datasets: Map from `climate_variable` to its :class:`Variable` row.
        forcings: Map from `climate_forcing` to its :class:`Forcing` row.
        scenarios: Map from `climate_scenario` to its :class:`Scenario` row.
        rounds: Map from `simulation_round` to its :class:`Round` row.

    Examples:
        - List curated variables and resolve one:
            ```python
            >>> from earthlens.isimip import Catalog
            >>> cat = Catalog()
            >>> "pr" in cat
            True
            >>> cat.get_dataset("pr").units
            'kg m-2 s-1'
            >>> cat.data_url
            'https://data.isimip.org/api/v1'

            ```
        - An unknown variable raises with a did-you-mean hint:
            ```python
            >>> from earthlens.isimip import Catalog
            >>> Catalog().get_dataset("rainfall")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: 'rainfall' is not in the ISIMIP catalog. Known variables: [...].

            ```
    """

    _catalog_kind: str = "ISIMIP catalog"
    _entry_noun: str = "variables"

    data_url: str = ""
    files_api_url: str = ""
    products: list[str] = Field(default_factory=list)
    time_steps: list[str] = Field(default_factory=list)

    datasets: dict[str, Variable] = Field(default_factory=dict)
    forcings: dict[str, Forcing] = Field(default_factory=dict)
    scenarios: dict[str, Scenario] = Field(default_factory=dict)
    rounds: dict[str, Round] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled catalog when no variables were supplied.

        `Catalog()` with no args reads :data:`CATALOG_PATH` (cached by
        `(path, mtime)`); passing `datasets=...` skips the disk read (used in
        tests). Either way the `available_datasets` index is derived from the
        loaded variable map.

        Raises:
            ValueError: Propagated from :meth:`load` when the YAML is missing,
                empty, or has a malformed row.
        """
        if not self.datasets and not self.data_url:
            loaded = Catalog.load()
            self.data_url = loaded.data_url
            self.files_api_url = loaded.files_api_url
            self.products = loaded.products
            self.time_steps = loaded.time_steps
            self.datasets = loaded.datasets
            self.forcings = loaded.forcings
            self.scenarios = loaded.scenarios
            self.rounds = loaded.rounds
        self.available_datasets = sorted(self.datasets)

    @classmethod
    def load(cls, catalog_path: Path | None = None) -> Catalog:
        """Read and validate the ISIMIP catalog from disk (cached).

        Args:
            catalog_path: Path to the catalog YAML. Defaults to the module-level
                :data:`CATALOG_PATH`.

        Returns:
            A fully-populated :class:`Catalog`.

        Raises:
            ValueError: If `catalog_path` does not exist, or if the file is
                missing its config keys, or any curated row fails validation.
        """
        path = catalog_path if catalog_path is not None else CATALOG_PATH
        payload = load_catalog(path, _CATALOG_CACHE, _parse_catalog, provider="ISIMIP")
        return cls(**payload)

    @staticmethod
    def _parse_block(path: Path, block: Any, model: type[BaseModel]) -> dict[str, Any]:
        """Validate one YAML mapping block into `{key: model(...)}`.

        Args:
            path: Catalog path, for error messages.
            block: The raw mapping from the YAML (or `None` when absent).
            model: The pydantic row type to build.

        Returns:
            dict[str, Any]: The validated rows keyed by their YAML key.

        Raises:
            ValueError: If any row fails validation.
        """
        out: dict[str, Any] = {}
        for key, body in (block or {}).items():
            try:
                out[str(key)] = model(**dict(body or {}))
            except ValidationError as exc:
                raise ValueError(
                    f"{path} {model.__name__} {key!r} failed validation:\n{exc}"
                ) from exc
        return out

    def get_forcing(self, key: str) -> Forcing:
        """Return the :class:`Forcing` for `key`, with a did-you-mean hint.

        Args:
            key: A `climate_forcing` (`"gfdl-esm4"`, `"ukesm1-0-ll"`).

        Returns:
            Forcing: The matching forcing row.

        Raises:
            ValueError: If `key` is not a curated forcing.
        """
        return cast("Forcing", self._get_from(self.forcings, key, "forcing"))

    def get_scenario(self, key: str) -> Scenario:
        """Return the :class:`Scenario` for `key`, with a did-you-mean hint.

        Args:
            key: A `climate_scenario` (`"ssp585"`, `"historical"`).

        Returns:
            Scenario: The matching scenario row.

        Raises:
            ValueError: If `key` is not a curated scenario.
        """
        return cast("Scenario", self._get_from(self.scenarios, key, "scenario"))

    def get_round(self, key: str) -> Round:
        """Return the :class:`Round` for `key`, with a did-you-mean hint.

        Args:
            key: A `simulation_round` (`"ISIMIP3b"`, `"ISIMIP3a"`).

        Returns:
            Round: The matching round row.

        Raises:
            ValueError: If `key` is not a curated round.
        """
        return cast("Round", self._get_from(self.rounds, key, "round"))

    @staticmethod
    def normalize_forcing(name: str) -> str:
        """Return the API spelling of a climate forcing (lowercased).

        The ISIMIP API spells `climate_forcing` lowercase (`"gfdl-esm4"`), but
        users often pass the CMIP6 model casing (`"GFDL-ESM4"`); normalise so
        both resolve.

        Args:
            name: A forcing name in any casing.

        Returns:
            str: The lowercased API spelling.

        Examples:
            - Normalise a CMIP6-cased model name:
                ```python
                >>> from earthlens.isimip import Catalog
                >>> Catalog.normalize_forcing("GFDL-ESM4")
                'gfdl-esm4'

                ```
        """
        return name.lower()

    @staticmethod
    def _get_from(mapping: dict[str, Any], key: str, noun: str) -> Any:
        """Look up `key` in `mapping`, raising a did-you-mean `ValueError`.

        Args:
            mapping: The curated map to search.
            key: The requested key.
            noun: Singular noun for the error message (`"forcing"`).

        Returns:
            The matching row.

        Raises:
            ValueError: If `key` is absent.
        """
        try:
            return mapping[key]
        except KeyError:
            import difflib

            close = difflib.get_close_matches(key, mapping, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"{key!r} is not a curated ISIMIP {noun}. "
                f"Known {noun}s: {sorted(mapping)}.{hint}"
            ) from None
