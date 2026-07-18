"""Shared fixtures for the GBIF backend tests — a faked `pygbif` module."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest


class _FakeOccurrences:
    """Stand-in for `pygbif.occurrences` recording `search` calls."""

    def __init__(self):
        """Start with one empty page and no recorded calls."""
        self.pages: list[dict] = [{"results": [], "count": 0, "endOfRecords": True}]
        self.calls: list[dict] = []

    def set_pages(self, pages: list[dict]) -> None:
        """Pin the sequence of page dicts `search` returns, one per call."""
        self.pages = pages

    def search(self, **kwargs):
        """Record the call and return the next configured page."""
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.pages) - 1)
        return self.pages[index]


class _FakeSpecies:
    """Stand-in for `pygbif.species` with a canned `name_backbone`."""

    def __init__(self):
        """Default to a Panthera leo backbone match (nested 0.6.6 shape)."""
        self.result: dict = {"usage": {"key": 5219404, "name": "Panthera leo"}}
        self.calls: list[str | None] = []

    def name_backbone(self, scientificName=None, **kwargs):
        """Mirror pygbif 0.6.6's signature (first param `scientificName`).

        Records the name actually received so a test can assert the backend
        passes it positionally — a regression to a `name=` kwarg would leave
        `scientificName` `None` and surface here.
        """
        self.calls.append(scientificName)
        return self.result


def _record(**fields):
    """Build a GBIF occurrence record dict with sensible defaults."""
    record = {
        "key": 1,
        "scientificName": "Aves",
        "taxonKey": 212,
        "decimalLatitude": 5.0,
        "decimalLongitude": 6.0,
        "eventDate": "2020-06-01",
        "basisOfRecord": "HUMAN_OBSERVATION",
        "datasetKey": "abc",
        "license": "CC0_1_0",
        "countryCode": "KE",
        "coordinateUncertaintyInMeters": 10.0,
    }
    record.update(fields)
    return record


@pytest.fixture
def fake_gbif(monkeypatch):
    """Install a fake `pygbif` module exposing `occurrences` and `species`."""
    occurrences = _FakeOccurrences()
    species = _FakeSpecies()
    module = ModuleType("pygbif")
    module.occurrences = occurrences
    module.species = species
    monkeypatch.setitem(sys.modules, "pygbif", module)
    return SimpleNamespace(occurrences=occurrences, species=species, record=_record)


@pytest.fixture
def log_messages():
    """Capture loguru INFO+ messages into a list for the duration of a test."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]))
    yield messages
    logger.remove(sink_id)
