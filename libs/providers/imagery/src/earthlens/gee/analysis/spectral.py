"""Sensor-specific spectral transforms over a reflectance band stack.

Every other operation in this subpackage is a generic GIS primitive that treats
its input as an anonymous array. `TasseledCap` is the exception: it rotates a
multi-band *reflectance* raster into the Kauth-Thomas brightness / greenness /
wetness components with a fixed linear combination whose coefficients are tied
to a particular sensor and a particular band order.

That sensor knowledge is domain value-semantics, not geometry, so it lives
upstream in pyramids-eo (`pyramids_eo.spectral.tasseled_cap`, with the
coefficient tables in its sensor registry) rather than here - this module only
wraps that transform as an `OperationChain`-composable operation. Because the
coefficients assume a band order, a silently wrong one yields a
plausible-looking but meaningless raster; pyramids-eo guards against it by
reordering named bands to the sensor's roles and, for unnamed bands, checking
the band count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["TasseledCap"]

#: TasseledCap's default components, in output-band order - the three classic
#: Kauth-Thomas components. `apply` passes the chosen components to pyramids-eo
#: explicitly, so this is earthlens's own default, not a mirror of any upstream one.
_DEFAULT_COMPONENTS: tuple[str, ...] = ("brightness", "greenness", "wetness")


class TasseledCap(RasterOperation):
    """Rotate a reflectance band stack into its tasseled-cap components.

    `apply` treats its argument as a stack of the sensor's reflectance bands and
    returns a raster whose bands are the requested `components` - brightness,
    greenness, wetness by default - carrying the input's grid and CRS. A thin
    wrapper over `pyramids_eo.spectral.tasseled_cap`, which owns the per-sensor
    coefficients (the GIS-agnostic, sensor-specific part) and the band-order
    validation.

    The input bands must be in the sensor's reflectance domain (a reflectance
    factor, not raw DN - a DN stack is warned about, not rejected) and in the
    sensor's band order. When the input's bands are named (`blue` / `red` / ...
    or a sensor spelling such as `B4` / `SR_B5`) they are matched to the
    sensor's roles and reordered; when they are unnamed only the band *count* is
    checked, so the stack is assumed to already be in the documented order.

    Examples:
        - Brightness / greenness / wetness from a Landsat 8 OLI stack:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> from earthlens.gee.analysis import TasseledCap
            >>> stack = np.ones((6, 1, 1), dtype="float32")  # blue..swir2 at 1.0
            >>> ds = Dataset.from_array(
            ...     arr=stack,
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=None,
            ... )
            >>> ds.band_names = ["blue", "green", "red", "nir", "swir1", "swir2"]
            >>> tc = TasseledCap(sensor="landsat8_oli")(ds)
            >>> tc.band_names
            ['brightness', 'greenness', 'wetness']
            >>> float(np.asarray(tc.read_array())[0, 0, 0].round(4))
            2.3099

            ```
        - Pick a single component:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> from earthlens.gee.analysis import TasseledCap
            >>> stack = np.ones((6, 1, 1), dtype="float32")
            >>> ds = Dataset.from_array(
            ...     arr=stack,
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=None,
            ... )
            >>> ds.band_names = ["blue", "green", "red", "nir", "swir1", "swir2"]
            >>> TasseledCap(sensor="landsat8_oli", components=("wetness",))(ds).band_names
            ['wetness']

            ```
    """

    sensor: str
    components: tuple[str, ...] = _DEFAULT_COMPONENTS

    def apply(self, ds: Dataset) -> Dataset:
        """Return the tasseled-cap components of `ds`.

        Args:
            ds: A reflectance band stack in the sensor's bands. Its band names,
                when present, drive band-order validation; its grid, CRS and
                per-band no-data are carried onto the result.

        Returns:
            A new raster of one band per requested component, in the order
            given, with NaN no-data; a pixel that is the input's no-data or
            non-finite in any band is NaN in every component.

        Raises:
            UnknownSensorError: If `sensor` has no bundled coefficient table
                (pyramids-eo's error, raised unchanged).
            TransformError: If `components` is empty or names a component the
                sensor does not define, or the input is not a band stack whose
                band count / names match the sensor (pyramids-eo's error).
        """
        from pyramids_eo.spectral import tasseled_cap

        return tasseled_cap(ds, sensor=self.sensor, components=list(self.components))
