"""Tests for the catalog-driven `preprocess()` recipe (Task 6.13)."""

from __future__ import annotations

import typing

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee.catalog import (  # noqa: E402
    Band,
    Catalog,
    Dataset,
    Extent,
    MaskRecipe,
    Preprocess,
)
from earthlens.gee.cloud_masks import (  # noqa: E402
    BandScaler,
    MaskChain,
    PassThrough,
    Sentinel2SclMask,
    SpeckleFilter,
)
from earthlens.gee.configs import EngineConfig  # noqa: E402
from earthlens.gee.recipes import (  # noqa: E402
    _MASK_RECIPES,
    _MASK_REQUIRED_BANDS,
    preprocess,
)


@pytest.fixture
def notes_buffer():
    """Collect loguru's INFO output, which pytest's `caplog` never sees.

    loguru writes through its own handler set, bound to the original stderr at
    registration time, so a dedicated sink is the only way to read it back.
    """
    from loguru import logger

    buffer: list[str] = []
    sink_id = logger.add(buffer.append, level="INFO")
    yield buffer
    logger.remove(sink_id)


class _FakeCatalog:
    """A catalog holding one row, for recipes the real YAML does not carry."""

    def __init__(self, row):
        self.row = row

    def get_dataset(self, dataset_id):
        return self.row


def _row(preprocess_block, bands=None):
    """A minimal catalog row carrying the given recipe."""
    return Dataset(
        id="X/Y",
        title="X",
        extent=Extent(start_date="2000-01-01"),
        bands=bands if bands is not None else {"b1": Band(id="b1", scale=2.0)},
        preprocess=preprocess_block,
    )


class TestPreprocessIsCatalogData:
    """The recipe is a typed catalog field, not an `if/elif` over asset ids."""

    def test_the_recipe_is_a_declared_field(self):
        """`Dataset.preprocess` parses into a `Preprocess`, not a dict."""
        row = _row({"mask": "sentinel2_scl", "scale": True})
        assert isinstance(row.preprocess, Preprocess)
        assert (row.preprocess.mask, row.preprocess.scale) == ("sentinel2_scl", True)

    def test_an_unknown_mask_name_is_refused(self):
        """A typo'd mask name fails at load time instead of being ignored."""
        with pytest.raises(ValidationError):
            _row({"mask": "sentinel2_scll"})

    def test_a_typo_inside_the_recipe_is_refused(self):
        """`extra="forbid"` on the recipe catches a misspelled key.

        `Dataset` itself does not forbid extras, which is exactly why the
        recipe has to be a declared field: an undeclared `preprocess:` key
        would have been dropped in silence.
        """
        with pytest.raises(ValidationError):
            _row({"mask": "sentinel2_scl", "scaled": True})

    def test_a_row_without_a_recipe_reads_as_none(self):
        """The field defaults to `None`, so most rows are unaffected."""
        assert _row(None).preprocess is None

    def test_the_recipe_names_match_the_resolver(self):
        """The `MaskRecipe` literal and `_MASK_RECIPES` cannot drift apart."""
        assert set(typing.get_args(MaskRecipe)) == set(_MASK_RECIPES)

    def test_the_recipe_is_frozen(self):
        """A recipe cannot be rewritten after the row is loaded."""
        with pytest.raises(ValidationError):
            Preprocess(mask="sentinel2_scl").scale = True


class TestPreprocessComposition:
    """Resolution order and shape, driven off a one-row fake catalog."""

    def test_mask_then_scale(self):
        """The mask runs first, so it reads the raw quality band."""
        catalog = _FakeCatalog(_row({"mask": "sentinel2_scl", "scale": True}))
        chain = preprocess("X/Y", catalog=catalog)
        assert isinstance(chain, MaskChain)
        assert [type(op).__name__ for op in chain.masks] == [
            "Sentinel2SclMask",
            "BandScaler",
        ]

    def test_a_mask_only_recipe_yields_one_step(self):
        """With `scale: false` the chain is just the mask."""
        catalog = _FakeCatalog(_row({"mask": "sentinel2_qa60"}))
        assert preprocess("X/Y", catalog=catalog).name == "Sentinel2Qa60Mask"

    def test_a_scale_only_recipe_yields_one_step(self):
        """A recipe naming no mask still scales."""
        catalog = _FakeCatalog(_row({"scale": True}))
        assert preprocess("X/Y", catalog=catalog).name == "BandScaler"

    def test_the_bands_argument_narrows_the_scaler(self):
        """The requested bands decide which factors the scaler carries."""
        row = _row(
            {"scale": True},
            bands={"a": Band(id="a", scale=2.0), "b": Band(id="b", scale=3.0)},
        )
        chain = preprocess("X/Y", ["b"], catalog=_FakeCatalog(row))
        assert [f.band for f in chain.masks[0].factors] == ["b"]

    def test_an_empty_recipe_is_an_explicit_pass_through(self):
        """Nothing to apply yields a documented no-op, not an empty chain."""
        catalog = _FakeCatalog(_row({"scale": False, "note": "already corrected"}))
        chain = preprocess("X/Y", catalog=catalog)
        assert [type(op).__name__ for op in chain.masks] == ["PassThrough"]

    def test_a_dataset_with_no_recipe_raises(self):
        """`preprocess` reports rather than silently returning an identity."""
        with pytest.raises(ValueError, match="no curated preprocessing recipe"):
            preprocess("X/Y", catalog=_FakeCatalog(_row(None)))

    def test_the_note_is_logged(self, notes_buffer):
        """The recipe's note reaches the user instead of sitting in the YAML."""
        catalog = _FakeCatalog(_row({"note": "speckle is the remaining gap"}))
        preprocess("X/Y", catalog=catalog)
        assert "speckle is the remaining gap" in "".join(notes_buffer)

    def test_the_default_chain_can_be_extended(self):
        """A user appends to the recipe rather than rebuilding it."""
        catalog = _FakeCatalog(_row({"note": "already corrected"}))
        extended = preprocess("X/Y", catalog=catalog) >> SpeckleFilter()
        assert extended.name == "PassThrough >> SpeckleFilter"

    def test_the_default_chain_can_be_replaced(self):
        """Nothing forces the recipe on a caller who wants their own chain."""
        own = Sentinel2SclMask() >> BandScaler()
        assert own.name == "Sentinel2SclMask >> BandScaler"

    def test_the_chain_forces_server_side_compute(self):
        """A recipe on the hook declines the EEDAI path, like any mask."""
        catalog = _FakeCatalog(_row({"mask": "sentinel2_scl", "scale": True}))
        chain = preprocess("X/Y", catalog=catalog)
        assert EngineConfig(cloud_mask=chain).needs_server_side_compute is True


class TestPassThrough:
    """The explicit no-op step of a recipe with nothing to apply."""

    def test_it_returns_the_image_untouched(self):
        """Identity, so the image's properties are trivially preserved."""
        sentinel = object()
        assert PassThrough()(sentinel) is sentinel


@pytest.fixture(scope="module")
def catalog():
    """The bundled GEE catalog."""
    return Catalog()


class TestShippedRecipes:
    """The recipes curated onto the real catalog rows."""

    @pytest.mark.parametrize(
        ("asset", "expected"),
        [
            ("COPERNICUS/S2_SR_HARMONIZED", "Sentinel2SclMask >> BandScaler"),
            ("COPERNICUS/S2_HARMONIZED", "Sentinel2Qa60Mask >> BandScaler"),
            ("LANDSAT/LC08/C02/T1_L2", "LandsatSrMask >> BandScaler"),
            ("LANDSAT/LC09/C02/T1_L2", "LandsatSrMask >> BandScaler"),
            ("LANDSAT/LE07/C02/T1_L2", "LandsatSrMask >> BandScaler"),
            ("MODIS/061/MOD09GA", "ModisState1kmMask >> BandScaler"),
            ("MODIS/061/MYD09GA", "ModisState1kmMask >> BandScaler"),
            ("COPERNICUS/S1_GRD", "PassThrough"),
        ],
    )
    def test_the_shipped_recipes_resolve(self, asset, expected, catalog):
        """Every curated recipe builds the chain its sensor needs."""
        assert preprocess(asset, catalog=catalog).name == expected

    def test_sentinel2_scales_to_reflectance(self, catalog):
        """The S2 L2A recipe carries the 0.0001 reflectance factor."""
        chain = preprocess("COPERNICUS/S2_SR_HARMONIZED", ["B4"], catalog=catalog)
        assert chain.masks[1].factors[0].scale == 0.0001

    def test_landsat_scales_and_offsets(self, catalog):
        """The Landsat C2-L2 recipe carries both halves of the conversion."""
        chain = preprocess("LANDSAT/LC08/C02/T1_L2", ["SR_B4"], catalog=catalog)
        factor = chain.masks[1].factors[0]
        assert (factor.scale, factor.offset) == (2.75e-05, -0.2)

    def test_sentinel1_documents_what_earth_engine_already_applied(self, catalog):
        """The S1 note names the upstream corrections and the speckle gap."""
        note = catalog.get_dataset("COPERNICUS/S1_GRD").preprocess.note
        assert "terrain correction" in note
        assert "SpeckleFilter" in note

    def test_every_recipe_names_a_resolvable_mask(self, catalog):
        """No curated row names a mask the resolver cannot build."""
        named = {
            row.preprocess.mask
            for row in catalog.datasets.values()
            if row.preprocess is not None and row.preprocess.mask is not None
        }
        assert named <= set(_MASK_RECIPES)


def _recipe_rows(catalog):
    """Every curated row carrying a `preprocess:` block, keyed by asset id."""
    return {
        asset: row
        for asset, row in catalog.datasets.items()
        if row.preprocess is not None
    }


def _missing_quality_bands(row):
    """Name the bands the row's recipe mask reads but the row does not declare."""
    recipe = row.preprocess
    required = (
        frozenset()
        if recipe is None or recipe.mask is None
        else _MASK_REQUIRED_BANDS[recipe.mask]
    )
    return sorted(required - set(row.bands))


class TestRecipeInvariants:
    """M5: the docstring's promises about curated recipes, locked by tests."""

    def test_the_required_band_map_covers_every_recipe_name(self):
        """Neither map can gain a recipe name without the other."""
        assert set(_MASK_REQUIRED_BANDS) == set(_MASK_RECIPES)

    def test_every_recipe_row_declares_its_masks_quality_band(self, catalog):
        """A recipe's mask reads a band the row must carry, or EE fails live."""
        offenders = {
            asset: _missing_quality_bands(row)
            for asset, row in _recipe_rows(catalog).items()
            if _missing_quality_bands(row)
        }
        assert offenders == {}

    def test_a_mis_curated_recipe_is_caught(self):
        """The check bites: a row without `SCL` is reported, not passed over."""
        row = Dataset(
            id="X/Y",
            title="no SCL here",
            extent=Extent(start_date="2020-01-01"),
            bands={"B4": Band(id="B4")},
            preprocess=Preprocess(mask="sentinel2_scl"),
        )
        assert _missing_quality_bands(row) == ["SCL"]

    def test_every_scaling_recipe_resolves_to_at_least_one_factor(self, catalog):
        """`scale: true` must not degrade to a warning-only identity scaler."""
        empty = [
            asset
            for asset, row in _recipe_rows(catalog).items()
            if row.preprocess.scale and not BandScaler.for_dataset(row).factors
        ]
        assert empty == []

    def test_a_scaling_recipe_on_a_factorless_row_is_caught(self):
        """The factor check bites too: no `scale`/`offset` means no factors."""
        row = Dataset(
            id="X/Y",
            title="no factors here",
            extent=Extent(start_date="2020-01-01"),
            bands={"B4": Band(id="B4")},
            preprocess=Preprocess(scale=True),
        )
        assert BandScaler.for_dataset(row).factors == ()

    def test_no_quality_band_declares_a_scale_or_offset(self, catalog):
        """Scaling a quality band would corrupt the very flags a mask reads."""
        corrupted = [
            f"{asset}:{band}"
            for asset, row in _recipe_rows(catalog).items()
            for band in _MASK_REQUIRED_BANDS.get(row.preprocess.mask or "", frozenset())
            if band in row.bands
            and (
                row.bands[band].scale is not None or row.bands[band].offset is not None
            )
        ]
        assert corrupted == []
