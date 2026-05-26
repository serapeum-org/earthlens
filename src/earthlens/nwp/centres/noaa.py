"""NOAA NODD centre — GRIB2 subset fetch via Herbie.

Herbie owns the `.idx` byte-range subsetting that cuts >99 % of the
download volume for the NOAA models (GFS / GEFS / HRRR / RAP / NAM /
…). :class:`NOAACentre` is the thin adapter: it maps the requested
earthlens params to a single Herbie `search` regex, builds the
mirror-priority list from the `mirror=` kwarg, and returns the local
path of the variable-subset GRIB2 that Herbie wrote.

Herbie is imported lazily inside :meth:`NOAACentre.fetch_one` (never at
module import) for two reasons: its import chain pulls `cfgrib` /
`eccodes` (the `[nwp]` extra + the eccodes binary), and its package
`__init__` prints a Unicode banner that crashes a cp1252 Windows
console — :func:`_import_herbie` captures that banner and rewrites a
missing-dependency import into a friendly `earthlens[nwp]` hint.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

from earthlens.nwp.centres.base import _NWPCentre

if TYPE_CHECKING:
    import datetime as dt

    from earthlens.nwp.catalog import NWPModel

#: Maps the earthlens `mirror=` kwarg to a Herbie source-priority key.
#: `"auto"` defers to the model's catalog `mirrors:` order.
_MIRROR_TO_HERBIE: dict[str, str] = {
    "aws": "aws",
    "gcp": "google",
    "google": "google",
    "azure": "azure",
    "origin": "nomads",
}


def _import_herbie() -> Any:
    """Import and return the `Herbie` class, guarding banner + missing deps.

    Returns:
        The `herbie.Herbie` class.

    Raises:
        ImportError: When `herbie` (or its `cfgrib` / `eccodes` import
            chain, including the eccodes binary library) is unavailable;
            the message names the `earthlens[nwp]` extra and the eccodes
            requirement.
    """
    try:
        # Herbie's package __init__ prints a Unicode banner; capture it so a
        # cp1252 console does not raise UnicodeEncodeError on import.
        with contextlib.redirect_stdout(io.StringIO()):
            from herbie import Herbie
    except ImportError as exc:
        raise ImportError(
            "the NWP NOAA centre needs `herbie-data`; install "
            "`pip install earthlens[nwp]`."
        ) from exc
    except RuntimeError as exc:
        # cfgrib/eccodes import the eccodes C library; the pip wheel cannot
        # always find it (notably on Windows). Surface the same install hint.
        raise ImportError(
            "Herbie imported but its cfgrib/eccodes stack could not load the "
            "eccodes C library. Install it via conda-forge (`eccodes`) — the "
            "pip `eccodes` wheel does not bundle the binary on every platform."
        ) from exc
    return Herbie


def _priority(mirror: str, model: NWPModel) -> list[str] | None:
    """Build Herbie's `priority` list from the `mirror=` kwarg (`G5`).

    Args:
        mirror: The `mirror=` kwarg (`"auto"`, `"aws"`, `"gcp"`,
            `"azure"`, `"origin"`).
        model: The resolved catalog row (its `mirrors:` order drives
            `"auto"`).

    Returns:
        list[str] | None: An explicit Herbie source-priority list, or
            `None` to let Herbie use its own default order.
    """
    if mirror == "auto":
        mapped = [_MIRROR_TO_HERBIE.get(m, m) for m in model.mirrors]
        return mapped or None
    return [_MIRROR_TO_HERBIE.get(mirror, mirror)]


class NOAACentre(_NWPCentre):
    """Herbie-backed fetcher for the NOAA NODD models."""

    def fetch_one(
        self,
        model: NWPModel,
        cycle: dt.datetime,
        step: int,
        params: list[str],
        mirror: str,
        member: str | None = None,
    ) -> Path:
        """Download the variable-subset GRIB2 for one `(cycle, step[, member])`.

        Joins the requested params' Herbie `search` regexes with `|`
        into a single `.idx` selector, runs `Herbie(...).download(...)`,
        and returns the path Herbie wrote.

        Args:
            model: The resolved catalog row.
            cycle: The forecast cycle datetime (UTC).
            step: The forecast lead time in hours.
            params: The requested earthlens parameter names.
            mirror: The selected cloud-mirror key.
            member: Ensemble member id (e.g. GEFS `"mean"` or `"5"`),
                forwarded to Herbie's `member=` — numeric ids become an
                `int`. `None` for a deterministic model.

        Returns:
            pathlib.Path: The local variable-subset GRIB2 file.
        """
        herbie_cls = _import_herbie()
        search = "|".join(model.bands[p] for p in params)
        kwargs: dict[str, Any] = {
            "model": model.model_family,
            "fxx": step,
            "priority": _priority(mirror, model),
            "save_dir": str(self.save_dir),
            "verbose": self.show_progress,
        }
        if model.product is not None:
            kwargs["product"] = model.product
        if member is not None:
            kwargs["member"] = int(member) if member.isdigit() else member
        # request_options carries any extra Herbie constructor kwargs a model
        # needs — e.g. `domain` for HiResW / HREF. Splat last so the catalog
        # row can override a default if it ever needs to.
        kwargs.update(model.request_options)
        handle = herbie_cls(cycle, **kwargs)
        return Path(handle.download(search))
