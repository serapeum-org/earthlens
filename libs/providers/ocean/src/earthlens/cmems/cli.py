"""Catalog-tooling handlers for the CMEMS (Copernicus Marine) backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._ocean_cli`). The light `refresher` / `prober`
use the public `copernicusmarine.describe`; the `deep_prober` opens the dataset
lazily to read its real NetCDF variables and needs
`COPERNICUSMARINE_SERVICE_USERNAME` / `_PASSWORD`.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import index_writer

#: Persists a live dataset-id fetch into the bundled `available_datasets:` block.
writer = index_writer("available_datasets")


def _describe() -> Any:
    """Return the live Copernicus Marine catalogue (SDK call, public)."""
    import copernicusmarine

    return copernicusmarine.describe(disable_progress_bar=True)


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List every CMEMS dataset id across the live catalogue (public SDK).

    Args:
        catalog: The loaded CMEMS `Catalog` (unused; the SDK is the source).

    Returns:
        A single-group mapping `{"cmems": [sorted dataset ids]}`.
    """
    result = _describe()
    ids = {
        did
        for product in getattr(result, "products", []) or []
        for dataset in getattr(product, "datasets", []) or []
        if (did := getattr(dataset, "dataset_id", None))
    }
    return {"cmems": sorted(str(i) for i in ids)}


def _describe_dataset(dataset_id: str) -> Any:
    """Return the live Copernicus Marine catalogue for one dataset (SDK)."""
    import copernicusmarine

    return copernicusmarine.describe(dataset_id=dataset_id, disable_progress_bar=True)


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a CMEMS dataset's variables (public `copernicusmarine.describe`).

    Walks the nested catalogue
    (`products[].datasets[].versions[].parts[].services[].variables[]`) and
    records each variable's standard name and units.

    Args:
        catalog: The loaded CMEMS `Catalog` (unused; the SDK is the source).
        dataset: The CMEMS dataset id.

    Returns:
        Mapping of variable short name to `{standard_name, units}`.
    """
    result = _describe_dataset(dataset)
    schema: dict[str, dict[str, Any]] = {}
    for product in getattr(result, "products", []) or []:
        for entry in getattr(product, "datasets", []) or []:
            for version in getattr(entry, "versions", []) or []:
                for part in getattr(version, "parts", []) or []:
                    for service in getattr(part, "services", []) or []:
                        for variable in getattr(service, "variables", []) or []:
                            name = getattr(variable, "short_name", None)
                            if name:
                                schema[str(name)] = {
                                    "standard_name": getattr(
                                        variable, "standard_name", None
                                    ),
                                    "units": getattr(variable, "units", None),
                                }
    return schema


def _deep_sample(dataset_id: str) -> dict[str, dict[str, Any]]:
    """Open a CMEMS dataset lazily and read its real NetCDF variables (creds)."""
    import copernicusmarine

    dataset = copernicusmarine.open_dataset(dataset_id=dataset_id)
    schema: dict[str, dict[str, Any]] = {}
    for name, variable in dataset.data_vars.items():
        attrs = variable.attrs
        schema[str(name)] = {
            "units": attrs.get("units"),
            "standard_name": attrs.get("standard_name"),
            "long_name": attrs.get("long_name"),
            "dtype": str(variable.dtype),
        }
    return schema


def deep_prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Deep-probe a CMEMS dataset's true NetCDF variable schema (credentialed).

    Unlike the light `describe` prober, this opens the dataset (lazily, no full
    download) to read the variable names / units / dtype as they actually appear
    in the served NetCDF. Needs `COPERNICUSMARINE_SERVICE_USERNAME` / `_PASSWORD`.

    Args:
        catalog: The loaded CMEMS `Catalog` (unused; the SDK is the source).
        dataset: The CMEMS dataset id.

    Returns:
        Mapping of variable name to `{units, standard_name, long_name, dtype}`.
    """
    return _deep_sample(dataset)
