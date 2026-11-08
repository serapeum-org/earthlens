"""Tests for the generic `QaBitsMask` / `qa_bits` QA-bit unpacking mask.

`ee` is faked with an *eager* recorder: a band carries a concrete list of QA
integers and `rightShift` / `bitwiseAnd` / `eq` / `Or` / `Not` evaluate the bit
arithmetic element-wise, so each test asserts exactly which pixel values the
mask keeps — proving the semantics, not merely the call shape.
"""

from __future__ import annotations

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee.cloud_masks import (  # noqa: E402
    ImageMask,
    MaskChain,
    QaBitsMask,
    qa_bits,
)


class _Bool:
    """An element-wise boolean band supporting `Or` / `Not`."""

    def __init__(self, values: list[bool]):
        self.values = values

    def Or(self, other: _Bool) -> _Bool:  # noqa: N802 - mirrors ee API
        return _Bool([a or b for a, b in zip(self.values, other.values, strict=True)])

    def Not(self) -> _Bool:  # noqa: N802 - mirrors ee API
        return _Bool([not v for v in self.values])


class _IntBand:
    """An element-wise integer band supporting `rightShift` / `bitwiseAnd` / `eq`."""

    def __init__(self, values: list[int]):
        self.values = values

    def rightShift(self, n: int) -> _IntBand:  # noqa: N802 - mirrors ee API
        return _IntBand([v >> n for v in self.values])

    def bitwiseAnd(self, m: int) -> _IntBand:  # noqa: N802 - mirrors ee API
        return _IntBand([v & m for v in self.values])

    def eq(self, value: int) -> _Bool:
        return _Bool([v == value for v in self.values])


class _FakeImage:
    """A one-band image whose `updateMask` captures the final boolean band."""

    def __init__(self, band: str, values: list[int]):
        self._band = band
        self._values = values
        self.kept: list[bool] | None = None

    def select(self, band: str) -> _IntBand:
        assert band == self._band, f"selected {band}, expected {self._band}"
        return _IntBand(self._values)

    def updateMask(self, mask: _Bool) -> _FakeImage:  # noqa: N802 - mirrors ee API
        self.kept = mask.values
        return self


class TestQaBitsMaskSemantics:
    """`QaBitsMask.__call__` keeps exactly the pixels its field selects."""

    def test_single_bit_keep(self):
        """A single flag bit (6) kept when set: only bit-6 pixels survive."""
        values = [0b0000000, 0b1000000, 0b1000001, 0b0001000]
        image = _FakeImage("QA_PIXEL", values)
        QaBitsMask(band="QA_PIXEL", bits=6, keep=(1,))(image)
        assert image.kept == [False, True, True, False], (
            f"bit-6 keep should survive 0b1000000/0b1000001, got {image.kept}"
        )

    def test_multi_bit_field_keep(self):
        """A 2-bit field at bits 8-9 keeps values 0 and 1, drops 2 and 3."""
        values = [0 << 8, 1 << 8, 2 << 8, 3 << 8]
        image = _FakeImage("QA_PIXEL", values)
        QaBitsMask(band="QA_PIXEL", bits=(8, 9), keep=(0, 1))(image)
        assert image.kept == [True, True, False, False], (
            f"field values 0/1 should be kept, 2/3 dropped, got {image.kept}"
        )

    def test_drop_polarity_is_the_complement(self):
        """`drop=(1,)` on bit 3 keeps every pixel whose bit 3 is clear."""
        values = [0b000, 0b1000, 0b1001, 0b0001]
        image = _FakeImage("QA_PIXEL", values)
        QaBitsMask(band="QA_PIXEL", bits=3, drop=(1,))(image)
        assert image.kept == [True, False, False, True], (
            f"bit-3 drop should remove 0b1000/0b1001, got {image.kept}"
        )

    def test_a_higher_field_ignores_lower_bits(self):
        """The field is shifted before compare, so low bits do not leak in."""
        values = [(1 << 9) | 0b11111111, 0b11111111]
        image = _FakeImage("state_1km", values)
        QaBitsMask(band="state_1km", bits=(8, 9), keep=(2,))(image)
        assert image.kept == [True, False], (
            f"only the bits-8-9==2 pixel should survive, got {image.kept}"
        )


class TestQaBitsMaskValidation:
    """Construction refuses ambiguous polarity or a malformed field."""

    def test_neither_keep_nor_drop_is_refused(self):
        """Without a polarity the mask is ambiguous, so it is refused."""
        with pytest.raises(ValidationError, match="exactly one of keep / drop"):
            QaBitsMask(band="QA_PIXEL", bits=6)

    def test_both_keep_and_drop_is_refused(self):
        """Giving both polarities is refused for the same reason."""
        with pytest.raises(ValidationError, match="exactly one of keep / drop"):
            QaBitsMask(band="QA_PIXEL", bits=6, keep=(1,), drop=(0,))

    def test_empty_keep_is_refused(self):
        """An empty keep set names no value, so it is refused."""
        with pytest.raises(ValidationError, match="exactly one of keep / drop"):
            QaBitsMask(band="QA_PIXEL", bits=6, keep=())

    def test_non_contiguous_bits_are_refused(self):
        """A gap in the bit run (8, 10) is not a single field."""
        with pytest.raises(ValidationError, match="contiguous ascending run"):
            QaBitsMask(band="QA_PIXEL", bits=(8, 10), keep=(0,))

    def test_descending_bits_are_refused(self):
        """Positions must ascend, so (9, 8) is refused."""
        with pytest.raises(ValidationError, match="contiguous ascending run"):
            QaBitsMask(band="QA_PIXEL", bits=(9, 8), keep=(0,))

    def test_negative_bit_is_refused(self):
        """A negative bit position is meaningless and refused."""
        with pytest.raises(ValidationError, match="non-negative"):
            QaBitsMask(band="QA_PIXEL", bits=-1, keep=(1,))

    def test_empty_bits_tuple_is_refused(self):
        """An empty bits tuple names no position, so it is refused."""
        with pytest.raises(ValidationError, match="at least one position"):
            QaBitsMask(band="QA_PIXEL", bits=(), keep=(0,))

    def test_negative_bit_inside_a_tuple_is_refused(self):
        """A negative position inside the tuple is refused like the single-int case."""
        with pytest.raises(ValidationError, match="non-negative"):
            QaBitsMask(band="QA_PIXEL", bits=(-1,), keep=(0,))

    def test_a_bool_bit_is_refused(self):
        """`bits=True` is not the integer 1 (NoBool guard)."""
        with pytest.raises(ValidationError, match="got the boolean"):
            QaBitsMask(band="QA_PIXEL", bits=True, keep=(1,))


class TestQaBitsFunction:
    """The `qa_bits` free function builds an equivalent mask that plugs in."""

    def test_low_and_width_are_derived(self):
        """A tuple field exposes its shift (low) and width."""
        mask = qa_bits("QA_PIXEL", bits=(8, 9), keep=(0, 1))
        assert (mask.band, mask.low, mask.width, mask.keep) == (
            "QA_PIXEL",
            8,
            2,
            (0, 1),
        )

    def test_accepts_any_iterable_for_keep(self):
        """`keep` / `bits` iterables are normalised to tuples."""
        mask = qa_bits("QA_PIXEL", bits=[8, 9], keep=[0, 1])
        assert mask.bits == (8, 9)
        assert mask.keep == (0, 1)

    def test_is_an_image_mask_and_composes(self):
        """The result is an `ImageMask`, so it chains with `>>`."""
        mask = qa_bits("QA_PIXEL", bits=6, keep=(1,))
        assert isinstance(mask, ImageMask)
        assert isinstance(mask >> qa_bits("SCL", bits=3, drop=(1,)), MaskChain)

    def test_drop_is_passed_through(self):
        """The `drop` polarity round-trips onto the model."""
        assert qa_bits("QA_PIXEL", bits=3, drop=(1,)).drop == (1,)
