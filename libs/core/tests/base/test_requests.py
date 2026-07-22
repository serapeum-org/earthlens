from __future__ import annotations

import pytest
from earthlens.base._requests import normalize_dataset_variables


class _DictBackend:
    """A dataset-keyed backend: `variables` carries the dataset in its keys."""

    def __init__(self, variables):
        self.variables = variables


class _NativeBackend:
    """A backend with a native `dataset` parameter (like S3)."""

    def __init__(self, dataset="default", variables=None):
        self.dataset = dataset
        self.variables = variables


class TestNormalizeDatasetVariables:
    """The dataset= / variables= split resolves per backend shape."""

    def test_dict_backend_composes_nested_dict(self):
        """A list + dataset becomes {dataset: variables} for a keyed backend."""
        out = normalize_dataset_variables(
            _DictBackend, "africa-monthly", ["precipitation"]
        )
        assert out == {"variables": {"africa-monthly": ["precipitation"]}}

    def test_native_backend_keeps_dataset_separate(self):
        """A backend with a native dataset param gets both kwargs separately."""
        out = normalize_dataset_variables(_NativeBackend, "sentinel-2", ["B04"])
        assert out == {"dataset": "sentinel-2", "variables": ["B04"]}

    def test_none_dataset_passes_list_through(self):
        """Without dataset, a list passes through unchanged."""
        assert normalize_dataset_variables(_DictBackend, None, ["precipitation"]) == {
            "variables": ["precipitation"]
        }

    def test_none_dataset_passes_dict_through(self):
        """Without dataset, the legacy nested dict passes through unchanged."""
        assert normalize_dataset_variables(
            _DictBackend, None, {"africa-monthly": ["precipitation"]}
        ) == {"variables": {"africa-monthly": ["precipitation"]}}

    def test_dict_variables_with_dataset_raises(self):
        """A dict variables + dataset is ambiguous for a keyed backend."""
        with pytest.raises(ValueError, match="pass variables= as a list"):
            normalize_dataset_variables(
                _DictBackend, "africa-monthly", {"africa-monthly": ["precipitation"]}
            )

    def test_dataset_without_variables_composes_empty_list(self):
        """dataset= with no variables= composes {dataset: []}, not a TypeError."""
        out = normalize_dataset_variables(_DictBackend, "africa-monthly", None)
        assert out == {"variables": {"africa-monthly": []}}, f"got {out}"

    def test_native_backend_dataset_without_variables(self):
        """A native-dataset backend forwards dataset= with variables=None."""
        out = normalize_dataset_variables(_NativeBackend, "sentinel-2", None)
        assert out == {"dataset": "sentinel-2", "variables": None}, f"got {out}"
