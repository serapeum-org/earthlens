"""ASF InSAR backend — SAR search and baseline `stack()` over `asf_search`.

`ASF(AbstractDataSource)` exposes two request shapes through one
interface: a plain space/time/platform SAR catalog search, and an
InSAR **baseline stack** built from a reference granule
(perpendicular- and temporal-baseline windowed). ASF SAR products
already live behind `earthlens.earthdata` for plain granule pulls,
but `asf_search`'s `ASFProduct.stack()` is the only path to the
coregistered set used for interferometry — that capability is what
this backend adds.

Search calls run anonymously; only the download step authenticates.
:class:`ASFAuth` composes the shipped :class:`EarthdataAuth` to mint
an EDL bearer token and hands it to `asf_search.ASFSession()` —
there is no second credential system. Set `EARTHDATA_TOKEN` /
`EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD`, or a
`urs.earthdata.nasa.gov` entry in `~/.netrc`, and the download
works.

`OUTPUT_KIND = "raster"`. `download()` returns the list of written
SAR product paths (SLC / BURST / RTC / GRD). The backend does
**not** crop or convert the products (an SLC is complex-valued —
not a plain bbox crop), so an `aggregate=` argument raises
`NotImplementedError`. Post-process the downloaded stack with a
dedicated InSAR tool.
"""

from __future__ import annotations

from earthlens.asf.auth import (
    ASFAuth,
    ASFCredentials,
    AuthenticationError,
)
from earthlens.asf.backend import ASF
from earthlens.asf.catalog import Catalog, Product

__all__ = [
    "ASF",
    "ASFAuth",
    "ASFCredentials",
    "AuthenticationError",
    "Catalog",
    "Product",
]
