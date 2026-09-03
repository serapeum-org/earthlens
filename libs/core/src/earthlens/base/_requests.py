"""Helpers for shaping a download request before it reaches a backend.

Kept separate from the facade so the request-shaping rules — currently
just the `dataset=` / `variables=` split — are unit-testable in isolation
and reusable by the one-shot functional entry point.
"""

from __future__ import annotations

from typing import Any

from earthlens.base.abstractdatasource import native_parameters


def normalize_dataset_variables(
    backend_cls: type,
    dataset: str | None,
    variables: dict[str, list[str]] | list[str] | None,
) -> dict[str, Any]:
    """Resolve `dataset=` + `variables=` into the kwargs a backend wants.

    EarthLens historically smuggled the dataset axis into the keys of a
    `variables` dict (`{"reanalysis-era5-single-levels": [...]}`). The
    facade now also accepts an explicit `dataset=` with a plain
    `variables` list. This helper reconciles the two for whichever
    backend is being constructed:

    * A backend that declares a native `dataset` parameter (e.g. the S3
      backend) receives `dataset` and `variables` as separate kwargs.
    * A dataset-keyed backend (ECMWF, GEE, CHC, …) with a list
      `variables` and an explicit `dataset` gets the composed
      `{dataset: variables}` dict.
    * A dataset-keyed backend with a dict `variables` and an explicit
      `dataset` is ambiguous and raises.
    * A dataset-keyed backend with an omitted `variables` (`None`) and an
      explicit `dataset` composes an empty `{dataset: []}` rather than
      tripping `list(None)`.
    * When `dataset` is `None`, `variables` is passed through unchanged,
      so the legacy nested-dict call keeps working untouched.

    Args:
        backend_cls: The concrete `AbstractDataSource` subclass about to
            be constructed. Its `__init__` signature is inspected for a
            native `dataset` parameter.
        dataset: The explicit dataset / collection key, or `None`.
        variables: The variable specification — a list of codes, a
            dataset-keyed dict, or `None`.

    Returns:
        The keyword arguments to splat into the backend constructor —
        always a `variables` entry, plus a `dataset` entry for backends
        that take one natively.

    Raises:
        ValueError: If `dataset` is given alongside a dict `variables`
            for a backend with no native `dataset` parameter.

    Examples:
        - A dataset-keyed backend composes the nested dict:
            ```python
            >>> class DictBackend:
            ...     def __init__(self, variables):
            ...         ...
            >>> normalize_dataset_variables(
            ...     DictBackend, "africa-monthly", ["precipitation"]
            ... )
            {'variables': {'africa-monthly': ['precipitation']}}

            ```
        - A backend with a native `dataset` keeps them separate:
            ```python
            >>> class NativeBackend:
            ...     def __init__(self, dataset="x", variables=None):
            ...         ...
            >>> normalize_dataset_variables(
            ...     NativeBackend, "sentinel-2", ["B04"]
            ... )
            {'dataset': 'sentinel-2', 'variables': ['B04']}

            ```
        - Without `dataset`, `variables` passes through unchanged:
            ```python
            >>> normalize_dataset_variables(DictBackend, None, ["precipitation"])
            {'variables': ['precipitation']}

            ```
    """
    if dataset is None:
        result: dict[str, Any] = {"variables": variables}
    elif "dataset" in native_parameters(backend_cls):
        result = {"dataset": dataset, "variables": variables}
    elif isinstance(variables, dict):
        raise ValueError(
            "pass variables= as a list when using dataset=, or omit "
            "dataset= and key the variables dict yourself"
        )
    else:
        # Mirror the AbstractDataSource.__init_subclass__ wrapper: an omitted
        # variables= alongside dataset= composes an empty list rather than
        # tripping `list(None)`.
        composed = list(variables) if variables is not None else []
        result = {"variables": {dataset: composed}}
    return result
