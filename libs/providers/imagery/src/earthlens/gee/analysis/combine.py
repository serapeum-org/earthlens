"""Operations that combine a raster with a companion raster.

Every other operation in this subpackage derives its output from a single
raster. These two take a second one, held as a field at construction and used
by `apply`:

* `PanSharpen` injects the spatial detail of a high-resolution panchromatic
  band into a coarser multispectral raster.
* `MatchHistogram` remaps a band so its distribution matches a reference
  acquisition's, which is how two scenes of one area are made radiometrically
  comparable before differencing.

Both delegate to pyramids (`Dataset.pan_sharpen` / `Dataset.match_histogram`,
pyramids 0.69.0), which owns the GDAL pan-sharpener and the histogram-matching
arithmetic - the GIS primitives - so this module carries none of it. The
companion raster is `arbitrary_types_allowed` for the same reason
`TrainingSet`'s arrays are: a pyramids `Dataset` is not a pydantic type.

The third member of the plan's Task 2.14, `TasseledCap`, is a sibling of these
two - an operation whose input is a raster - but its per-sensor coefficients
over a fixed band order are domain value-semantics rather than a GIS primitive,
so it lives in its own module, `analysis.spectral`, and delegates to pyramids-eo
(pyramids-eo#117) rather than to a pyramids `Dataset` method.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import ConfigDict

from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["MatchHistogram", "PanSharpen"]


class PanSharpen(RasterOperation):
    """Fuse a multispectral raster with a finer panchromatic band.

    `apply` treats its argument as the coarse multispectral source and fuses it
    with `pan`, the high-resolution single-band detail source, producing a
    raster at the pan resolution that keeps the multispectral colour
    relationships. A thin wrapper over `Dataset.pan_sharpen`, which is pyramids'
    facade over GDAL's native pan-sharpener (`gdal.CreatePansharpenedVRT`); the
    only algorithm GDAL implements is the weighted Brovey transform.

    The multispectral source and `pan` must share a CRS and overlap in space.

    Examples:
        - Sharpen a 3-band multispectral raster with a 4x-finer pan band:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> rng = np.random.default_rng(0)
            >>> ms = Dataset.from_array(
            ...     arr=rng.integers(0, 4000, size=(3, 16, 16)).astype("uint16"),
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.02, epsg=4326),
            ...     no_data_value=None,
            ... )
            >>> pan = Dataset.from_array(
            ...     arr=rng.integers(0, 4000, size=(64, 64)).astype("uint16"),
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.005, epsg=4326),
            ...     no_data_value=None,
            ... )
            >>> fused = PanSharpen(pan=pan)(ms)
            >>> (fused.rows, fused.columns, fused.band_count)
            (64, 64, 3)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    pan: Any  # a pyramids Dataset; Any so the annotation stays import-free
    algorithm: Literal["brovey"] = "brovey"
    weights: tuple[float, ...] | None = None
    resampling: Literal[
        "nearest", "bilinear", "cubic", "cubicspline", "lanczos", "average"
    ] = "cubic"
    bands: tuple[int, ...] | None = None
    nodata: float | None = None

    def apply(self, ds: Dataset) -> Dataset:
        """Return the pan-sharpened raster.

        Args:
            ds: The multispectral (coarse) source.

        Returns:
            A new multi-band raster at the pan resolution, one band per selected
            multispectral band, in `ds`' CRS.

        Raises:
            ValueError: If `weights` does not match the number of selected
                bands, or `bands` is empty or out of range.
            AlignmentError: If `pan` has a different CRS from `ds` or does not
                overlap it (pyramids' error, raised unchanged).
        """
        return ds.pan_sharpen(
            self.pan,
            algorithm=self.algorithm,
            weights=list(self.weights) if self.weights is not None else None,
            resampling=self.resampling,
            bands=list(self.bands) if self.bands is not None else None,
            nodata=self.nodata,
        )


class MatchHistogram(RasterOperation):
    """Remap a band so its distribution matches a reference acquisition's.

    `apply` maps every value of its argument's `band` to the `reference` value
    at the same cumulative probability, which is the usual way to make two
    acquisitions of one scene radiometrically comparable before differencing. A
    thin wrapper over `Dataset.match_histogram`; pyramids owns the CDF
    arithmetic and excludes no-data from **both** distributions rather than
    treating the sentinel as a value, writing voids back as the source band's
    no-data.

    Examples:
        - Shift a dim band onto a bright reference's distribution:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> rng = np.random.default_rng(0)
            >>> geo = GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326)
            >>> src = Dataset.from_array(
            ...     arr=rng.normal(50, 10, (8, 8)).astype("float32"), geo_ref=geo
            ... )
            >>> ref = Dataset.from_array(
            ...     arr=rng.normal(200, 30, (8, 8)).astype("float32"), geo_ref=geo
            ... )
            >>> matched = MatchHistogram(reference=ref)(src)
            >>> bool(np.asarray(matched.read_array()).mean() > 150)
            True

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    reference: Any  # a pyramids Dataset; Any so the annotation stays import-free
    band: int = 0
    reference_band: int | None = None
    bins: int = 256

    def apply(self, ds: Dataset) -> Dataset:
        """Return a raster whose `band` matches the reference's distribution.

        Args:
            ds: The source raster to remap.

        Returns:
            A new raster of the matched band, voids preserved as no-data.

        Raises:
            ValueError: If `band` or `reference_band` is out of range, or
                `bins` is not positive (pyramids' error, raised unchanged).
        """
        return ds.match_histogram(
            self.reference,
            band=self.band,
            reference_band=self.reference_band,
            bins=self.bins,
        )
