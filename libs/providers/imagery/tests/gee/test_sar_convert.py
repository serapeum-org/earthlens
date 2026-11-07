"""Tests for the SAR dB <-> linear-power conversions (`ToPower` / `ToDb`).

`ee` is faked with an *eager numeric* image: bands hold concrete floats and
`divide` / `pow` / `log10` / `multiply` compute element-wise, so each test
asserts the actual converted values (e.g. -10 dB -> 0.1 power), not the op
shape. `cloud_masks.ee` is monkeypatched so `ee.Image(10)` is the numeric
constant the power conversion raises.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

pytest.importorskip("ee")

from earthlens.gee import cloud_masks  # noqa: E402
from earthlens.gee.cloud_masks import (  # noqa: E402
    ImageMask,
    ToDb,
    ToPower,
    to_db,
    to_power,
)


class _Img:
    """An eager numeric image: {band: [floats]} plus a properties dict."""

    def __init__(self, bands: dict[str, list[float]], props: dict | None = None):
        self.bands = {b: list(v) for b, v in bands.items()}
        self.props = dict(props) if props else {}

    def divide(self, n: float) -> _Img:
        return _Img(
            {b: [v / n for v in vs] for b, vs in self.bands.items()}, self.props
        )

    def multiply(self, n: float) -> _Img:
        return _Img(
            {b: [v * n for v in vs] for b, vs in self.bands.items()}, self.props
        )

    def log10(self) -> _Img:
        return _Img(
            {b: [math.log10(v) for v in vs] for b, vs in self.bands.items()}, self.props
        )

    def pow(self, other: _Img) -> _Img:  # noqa: A003 - mirrors ee API
        base = self.bands["__const__"][0]
        return _Img(
            {b: [base**e for e in vs] for b, vs in other.bands.items()}, other.props
        )

    def select(self, band: str) -> _Img:
        return _Img({band: self.bands[band]}, self.props)

    def rename(self, band: str) -> _Img:
        (vs,) = self.bands.values()
        return _Img({band: vs}, self.props)

    def addBands(self, other: _Img, overwrite: bool = False) -> _Img:  # noqa: N802
        merged = {**self.bands, **other.bands}
        return _Img(merged, self.props)

    def propertyNames(self) -> list[str]:  # noqa: N802 - mirrors ee API
        return list(self.props)

    def copyProperties(self, source: _Img, properties=None) -> _Img:  # noqa: N802
        return _Img(self.bands, source.props)


@pytest.fixture(autouse=True)
def _fake_ee(monkeypatch):
    """Point `cloud_masks.ee.Image(c)` at the numeric constant image."""
    monkeypatch.setattr(
        cloud_masks,
        "ee",
        SimpleNamespace(
            Image=lambda c: (
                c if isinstance(c, _Img) else _Img({"__const__": [float(c)]})
            )
        ),
    )


class TestToPower:
    """`ToPower` applies 10 ** (dB / 10)."""

    def test_converts_decibels_to_power(self):
        """-10 dB is 0.1 and -20 dB is 0.01 in linear power."""
        out = ToPower()(_Img({"VV": [-10.0, -20.0, 0.0]}))
        assert out.bands["VV"] == pytest.approx([0.1, 0.01, 1.0]), (
            f"10**(dB/10) expected [0.1, 0.01, 1.0], got {out.bands['VV']}"
        )

    def test_only_named_bands_are_converted(self):
        """A band subset leaves the other bands untouched."""
        out = ToPower(bands=("VV",))(_Img({"VV": [-10.0], "angle": [30.0]}))
        assert out.bands["VV"] == pytest.approx([0.1]), "VV should convert"
        assert out.bands["angle"] == [30.0], "angle must be left alone"


class TestToDb:
    """`ToDb` applies 10 * log10(power)."""

    def test_converts_power_to_decibels(self):
        """0.1 power is -10 dB and 0.01 is -20 dB."""
        out = ToDb()(_Img({"VV": [0.1, 0.01, 1.0]}))
        assert out.bands["VV"] == pytest.approx([-10.0, -20.0, 0.0]), (
            f"10*log10(power) expected [-10, -20, 0], got {out.bands['VV']}"
        )

    def test_only_named_bands_are_converted(self):
        """A band subset leaves the other bands untouched."""
        out = ToDb(bands=("VV",))(_Img({"VV": [0.1], "angle": [30.0]}))
        assert out.bands["VV"] == pytest.approx([-10.0])
        assert out.bands["angle"] == [30.0]


class TestRoundTripAndPlumbing:
    """The pair round-trips, preserves properties, and plugs into the hook."""

    def test_to_db_of_to_power_is_identity(self):
        """Converting to power and back recovers the original dB values."""
        original = _Img({"VV": [-7.5, -15.0, -3.0]})
        back = to_db(to_power(original))
        assert back.bands["VV"] == pytest.approx([-7.5, -15.0, -3.0]), (
            f"round-trip should be the identity, got {back.bands['VV']}"
        )

    def test_properties_are_preserved(self):
        """`system:time_start` and friends survive the conversion."""
        out = to_power(_Img({"VV": [-10.0]}, props={"system:time_start": 123, "x": 1}))
        assert out.props == {"system:time_start": 123, "x": 1}, (
            f"properties must survive, got {out.props}"
        )

    def test_free_functions_accept_a_band_subset(self):
        """The `to_power` / `to_db` functions forward a `bands=` subset."""
        out = to_power(_Img({"VV": [-10.0], "angle": [30.0]}), bands=["VV"])
        assert out.bands["angle"] == [30.0] and out.bands["VV"] == pytest.approx([0.1])

    def test_are_image_masks(self):
        """Both are ImageMask ops, so they ride the cloud_mask= hook."""
        assert isinstance(ToPower(), ImageMask) and isinstance(ToDb(), ImageMask)
