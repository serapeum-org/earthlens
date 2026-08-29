"""URL builder for the JRC European flood-hazard backend.

The JRC serves the European Flood Hazard Map (EFHM) over a deterministic,
anonymous HTTPS directory (verified live 2026-08-09): one whole-Europe GeoTIFF
per return period at `{BASE_URL}/Europe_RP{rp}_filled_depth.tif`. Each file is a
single-band EPSG:4326 Float32 grid at ~0.000833 deg (~90 m; documented 100 m),
covering Europe and the Mediterranean Basin — 110162x51992 px (~23 GB
uncompressed), so it is **never** read whole. The backend opens it lazily over
`pyramids.dataset.Dataset.crop(bbox=)`, whose windowed fast path reads only the
AOI's pixel window over GDAL's `/vsicurl` (HTTP range requests) for an
axis-aligned box in the source CRS — the case here (an EPSG:4326 AOI against the
4326 EFHM). This module only builds the per-return-period URL; the windowed read
+ crop live in the backend, which wraps them in `vsicurl_config()` for the
`/vsicurl` readdir-suppression + retry/timeout tuning.
"""

from __future__ import annotations

#: Root of the JRC CEMS-EFAS flood-hazard directory (anonymous HTTPS, no auth).
BASE_URL: str = (
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/flood_hazard"
)

#: `strftime`-free file-name template; `{rp}` is the integer return period.
FILENAME_TEMPLATE: str = "Europe_RP{rp}_filled_depth.tif"


def efhm_url(
    rp: int, *, base_url: str = BASE_URL, template: str = FILENAME_TEMPLATE
) -> str:
    """Build the EFHM GeoTIFF URL for one return period.

    Args:
        rp: The integer return period in years (e.g. `100`).
        base_url: The directory root; defaults to `BASE_URL`.
        template: The file-name template; defaults to `FILENAME_TEMPLATE`.

    Returns:
        str: The fully-qualified `.tif` URL.

    Examples:
        - The verified RP100 URL:
            ```python
            >>> from earthlens.jrc._helpers import efhm_url
            >>> efhm_url(100)
            'https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/flood_hazard/Europe_RP100_filled_depth.tif'

            ```
    """
    return f"{base_url}/{template.format(rp=rp)}"
