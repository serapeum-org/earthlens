"""Cross-backend AbstractCatalog contract checks.

Holds the rules that must be true of every backend catalog at once, each
parametrized over the registry rather than a hand-kept list, so a new
backend is covered the moment it ships.

* `model_post_init` chains to `super()`, so the base `catalog` field is
  populated from `get_catalog()` — a backend that overrode it and left
  `catalog` empty was the original regression here.
* Rows are frozen, since the parse cache shares them across callers.
* `resolve` / `get_variable` are overridden or raise, and the
  did-you-mean errors name the backend's own entry noun.
* Every `_summary_fields` name a `SummarisedLeaf` row declares is real,
  and every reachable row renders as one line naming its class.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
from typing import Any

import pytest
import yaml
from loguru import logger
from pydantic import BaseModel

import earthlens.base
from earthlens._backends import discover_backends
from earthlens.base import AbstractCatalog
from earthlens.base.abstractdatasource import _WARNED_EMPTY_CATALOGS
from earthlens.base.leaves import SummarisedLeaf

#: The class names a backend's `catalog` module may expose, in preference order.
CATALOG_CLASS_NAMES = ("Catalog", "StationCatalog")


def _discover_catalogs() -> list[tuple[str, str]]:
    """Find `(module, class-name)` for every backend that ships a catalog.

    Discovered from the backend registry rather than listed by hand. A
    hardcoded list only covers the backends someone remembered to add, and the
    default `get_catalog()` fails *open* — a catalog that kept its rows
    somewhere other than `datasets` would return an empty mapping rather than
    raise, so the contract has to be checked on all of them, not a sample.

    Returns:
        list[tuple[str, str]]: Sorted `(module name, class name)` pairs.
    """
    pairs = []
    for package in {entry[0] for entry in discover_backends().values()}:
        module_name = f"{package}.catalog"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        for name in CATALOG_CLASS_NAMES:
            if hasattr(module, name):
                pairs.append((module_name, name))
                break
    return sorted(pairs)


#: (module, catalog-class-name) for every backend that ships a catalog.
CATALOG_BACKENDS = _discover_catalogs()


def _build(module_name: str, class_name: str):
    """Import and instantiate a backend's catalog from the bundled YAML."""
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


class _RowsKeptElsewhere(AbstractCatalog):
    """A catalog that does not keep its rows in `datasets`, as a subclass may."""


class _Row(BaseModel):
    """A pydantic row, the shape nearly every backend catalog stores."""

    name: str
    note: str | None = None


class _ModelRowCatalog(AbstractCatalog[_Row]):
    """A catalog whose rows are pydantic models."""


class _PlainRowCatalog(AbstractCatalog[Any]):
    """A catalog whose rows are plain mappings, which the base must also render."""


@pytest.fixture
def a_first_read():
    """Empty the once-per-class warning registry, then put back what was there.

    The registry is module-level state, so without this a test asserting on the
    first read of an empty catalog would depend on what ran before it, and would
    not survive being run twice in one session.
    """
    before = set(_WARNED_EMPTY_CATALOGS)
    _WARNED_EMPTY_CATALOGS.clear()
    yield
    _WARNED_EMPTY_CATALOGS.clear()
    _WARNED_EMPTY_CATALOGS.update(before)


def test_an_empty_catalog_warns_instead_of_answering_silently(a_first_read):
    """An out-of-tree subclass keeping rows elsewhere gets told, not ignored."""
    messages: list[str] = []
    handler = logger.add(
        lambda m: messages.append(m.record["message"]), level="WARNING"
    )
    try:
        assert _RowsKeptElsewhere().get_catalog() == {}
    finally:
        logger.remove(handler)
    assert any("override get_catalog()" in m for m in messages), messages
    assert len(messages) == 1, f"expected one warning, got {len(messages)}"


def test_the_empty_catalog_warning_does_not_repeat_per_access(a_first_read):
    """`catalog` is recomputed per read, so warning per call means one per loop."""

    class _AlsoElsewhere(AbstractCatalog):
        """A second empty catalog, so the once-per-class registry is exercised."""

    messages: list[str] = []
    handler = logger.add(
        lambda m: messages.append(m.record["message"]), level="WARNING"
    )
    try:
        catalog = _AlsoElsewhere()
        for _ in range(3):
            assert catalog.catalog == {}
        for _ in range(2):
            assert catalog.get_catalog() == {}
    finally:
        logger.remove(handler)
    assert len(messages) == 1, f"five reads produced {len(messages)} warnings"


def test_str_dumps_the_rows_as_yaml_without_their_empty_fields():
    """`__str__` is the human view of the rows, so unset fields are noise."""
    dumped = str(_ModelRowCatalog(datasets={"a": _Row(name="alpha")}))
    assert yaml.safe_load(dumped) == {"a": {"name": "alpha"}}, dumped
    assert "note" not in dumped, f"an unset field should be omitted; got {dumped!r}"


def test_str_renders_a_row_that_is_not_a_pydantic_model():
    """A subclass may store plain mappings, which have no `model_dump`."""
    dumped = str(_PlainRowCatalog(datasets={"a": {"name": "alpha"}}))
    assert yaml.safe_load(dumped) == {"a": {"name": "alpha"}}, dumped


def test_str_keeps_insertion_order_rather_than_sorting():
    """Catalog files are curated in a deliberate order, and the dump keeps it."""
    catalog = _ModelRowCatalog(
        datasets={"zulu": _Row(name="z"), "alpha": _Row(name="a")}
    )
    order = list(yaml.safe_load(str(catalog)))
    assert order == ["zulu", "alpha"], f"the dump reordered the rows: {order}"


def test_str_writes_non_ascii_text_as_itself():
    """Escaping a place name into ASCII escape sequences makes the dump unreadable."""
    dumped = str(_ModelRowCatalog(datasets={"a": _Row(name="Åland")}))
    assert "Åland" in dumped, f"non-ASCII text was escaped: {dumped!r}"


def test_getitem_translates_the_did_you_mean_error_into_a_keyerror():
    """The dict surface has to raise what `dict` raises, not `get_dataset`'s error."""
    catalog = _ModelRowCatalog(datasets={"alpha": _Row(name="a")})
    assert catalog["alpha"].name == "a"
    with pytest.raises(KeyError) as excinfo:
        catalog["alfa"]
    assert excinfo.value.args == ("alfa",), excinfo.value.args
    assert isinstance(excinfo.value.__cause__, ValueError), (
        "the did-you-mean message must survive as the cause"
    )


def test_repr_reports_counts_not_contents():
    """A catalog holds hundreds of rows, so a repr that dumps them is unusable."""
    catalog = _ModelRowCatalog(
        datasets={"alpha": _Row(name="a")}, available_datasets=["x", "y", "z"]
    )
    rendered = repr(catalog)
    assert rendered == "_ModelRowCatalog(datasets=1, available_datasets=3)", rendered


def test_get_provider_names_the_near_miss_it_found():
    """A slug is easy to mistype, and the registry is the only place to check it."""
    catalog = _ModelRowCatalog(providers={"ucsb-chc": {"name": "CHC"}})
    assert catalog.get_provider("ucsb-chc") == {"name": "CHC"}
    with pytest.raises(ValueError, match="Did you mean 'ucsb-chc'") as excinfo:
        catalog.get_provider("ucsb-chp")
    assert "Known providers: ['ucsb-chc']" in str(excinfo.value), excinfo.value


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_catalog_field_is_populated(module_name: str, class_name: str):
    """The base `catalog` field is non-empty after construction."""
    cat = _build(module_name, class_name)
    assert len(cat.catalog) > 0


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_catalog_mirrors_get_catalog(module_name: str, class_name: str):
    """`catalog` reads back exactly what `get_catalog()` returns."""
    cat = _build(module_name, class_name)
    assert dict(cat.catalog) == dict(cat.get_catalog())


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_catalog_is_a_read_only_view(module_name: str, class_name: str):
    """Writing through `catalog` fails instead of rewriting `datasets`."""
    cat = _build(module_name, class_name)
    key = next(iter(cat.datasets))
    with pytest.raises(TypeError):
        cat.catalog[key] = None
    assert cat.datasets[key] is not None, "datasets must be untouched"


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_dict_surface_matches_datasets(module_name: str, class_name: str):
    """The inherited len/in/iter surface is backed by the populated datasets."""
    cat = _build(module_name, class_name)
    assert len(cat) == len(cat.datasets)
    assert set(cat) == set(cat.datasets)
    first = next(iter(cat.datasets))
    assert first in cat


def test_every_backend_ships_a_discoverable_catalog():
    """Discovery must find one per backend package, not a subset.

    If this drops, a backend's catalog stopped being importable or renamed its
    class, and every contract check below silently stopped covering it.
    """
    packages = {entry[0] for entry in discover_backends().values()}
    assert len(CATALOG_BACKENDS) == len(packages), (
        f"{len(packages)} backend packages but {len(CATALOG_BACKENDS)} catalogs "
        f"discovered"
    )


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_get_catalog_is_not_silently_empty(module_name: str, class_name: str):
    """The inherited default returns `datasets`, which must actually hold rows.

    The base implementation cannot tell "this catalog has no rows" from "this
    catalog keeps its rows elsewhere", so it answers `{}` for both. Asserting
    non-empty across every backend is what turns that fail-open into a failure.
    """
    cat = _build(module_name, class_name)
    assert len(cat.get_catalog()) > 0, (
        f"{module_name}.{class_name}.get_catalog() is empty; if the rows live "
        f"somewhere other than `datasets`, override get_catalog()"
    )


#: Collection backends that keep a domain `available_collections` index and
#: mirror it into the base `available_datasets` field. `"list"` backends carry
#: a flat list; `"dict"` (stac) carries a per-endpoint dict that is flattened.
COLLECTION_INDEX_BACKENDS = [
    ("earthlens.openeo.catalog", "list"),
    ("earthlens.sentinel_hub.catalog", "list"),
    ("earthlens.stac.catalog", "dict"),
]


@pytest.mark.parametrize("module_name, shape", COLLECTION_INDEX_BACKENDS)
def test_available_datasets_mirrors_collection_index(module_name: str, shape: str):
    """The base available_datasets field mirrors the domain collection index."""
    cat = _build(module_name, "Catalog")
    assert cat.available_datasets
    if shape == "list":
        assert set(cat.available_datasets) == set(cat.available_collections)
    else:
        flat = {cid for ids in cat.available_collections.values() for cid in ids}
        assert set(cat.available_datasets) == flat


#: Backends that populate the base `providers` field (some via a domain alias:
#: earthdata mirrors `daacs`, stac mirrors `endpoints`, fdsn mirrors `datasets`).
PROVIDER_BACKENDS = [
    "earthlens.ecmwf.catalog",
    "earthlens.gee.catalog",
    "earthlens.earthdata.catalog",
    "earthlens.stac.catalog",
    "earthlens.fdsn.catalog",
]


@pytest.mark.parametrize("module_name", PROVIDER_BACKENDS)
def test_providers_populated_and_resolvable(module_name: str):
    """The base providers field is non-empty and get_provider() resolves a slug."""
    cat = _build(module_name, "Catalog")
    assert cat.providers
    slug = next(iter(cat.providers))
    assert cat.get_provider(slug) is cat.providers[slug]


#: Backends with no resolve step inherit the base stub (raises).
NO_RESOLVE_BACKENDS = [
    "earthlens.firms.catalog",
    "earthlens.gdacs.catalog",
    "earthlens.cmems.catalog",
]
#: Backends that override resolve() (signatures vary by backend need).
RESOLVE_BACKENDS = [
    "earthlens.ghsl.catalog",
    "earthlens.nwp.catalog",
    "earthlens.usgs_water.catalog",
    "earthlens.stac.catalog",
    "earthlens.openeo.catalog",
    "earthlens.sentinel_hub.catalog",
    "earthlens.earthdata.catalog",
    "earthlens.eumetsat.catalog",
]


@pytest.mark.parametrize("module_name", NO_RESOLVE_BACKENDS)
def test_resolve_default_raises(module_name: str):
    """A backend with no resolve step inherits the raising base stub."""
    cat = _build(module_name, "Catalog")
    with pytest.raises(NotImplementedError):
        cat.resolve("anything")


@pytest.mark.parametrize("module_name", RESOLVE_BACKENDS)
def test_resolve_is_overridden(module_name: str):
    """Backends with a resolve step override the base stub."""
    cat = _build(module_name, "Catalog")
    assert type(cat).resolve is not AbstractCatalog.resolve


#: Two-level backends expose the shared 2-arg get_variable(dataset, leaf).
LEAF_BACKENDS = [
    "earthlens.chc.catalog",
    "earthlens.ecmwf.catalog",
    "earthlens.cmems.catalog",
    "earthlens.gee.catalog",
    "earthlens.firms.catalog",
    "earthlens.tropycal.catalog",
]
#: Single-level backends (one row is the leaf) inherit the raising base stub.
NO_LEAF_BACKENDS = [
    "earthlens.fdsn.catalog",
    "earthlens.gdacs.catalog",
    "earthlens.overture.catalog",
]


@pytest.mark.parametrize("module_name", LEAF_BACKENDS)
def test_get_variable_is_two_arg_override(module_name: str):
    """Two-level catalogs override get_variable with the 2-arg contract."""
    cat = _build(module_name, "Catalog")
    assert type(cat).get_variable is not AbstractCatalog.get_variable


@pytest.mark.parametrize("module_name", NO_LEAF_BACKENDS)
def test_get_variable_default_raises(module_name: str):
    """Single-level catalogs inherit the raising base get_variable stub."""
    cat = _build(module_name, "Catalog")
    with pytest.raises(NotImplementedError):
        cat.get_variable("dataset", "leaf")


def test_firms_get_variable_aliases_get_column():
    """firms.get_variable returns the same leaf as get_column."""
    _earthlens_firms = pytest.importorskip("earthlens.firms")
    Catalog = _earthlens_firms.Catalog

    cat = Catalog()
    assert cat.get_variable("MODIS_NRT", "confidence") is cat.get_column(
        "MODIS_NRT", "confidence"
    )


def test_tropycal_get_variable_aliases_get_field():
    """tropycal.get_variable returns the same leaf as get_field."""
    _earthlens_tropycal = pytest.importorskip("earthlens.tropycal")
    Catalog = _earthlens_tropycal.Catalog

    cat = Catalog()
    assert cat.get_variable("north_atlantic", "mslp") is cat.get_field(
        "north_atlantic", "mslp"
    )


def test_earthdata_providers_is_a_copy_of_daacs():
    """earthdata providers mirrors daacs by content but is a distinct dict."""
    _earthlens_earthdata = pytest.importorskip("earthlens.earthdata")
    Catalog = _earthlens_earthdata.Catalog

    cat = Catalog()
    assert cat.providers == cat.daacs
    assert cat.providers is not cat.daacs


def test_stac_providers_is_a_copy_of_endpoints():
    """stac providers mirrors endpoints by content but is a distinct dict."""
    _earthlens_stac = pytest.importorskip("earthlens.stac")
    Catalog = _earthlens_stac.Catalog

    cat = Catalog()
    assert cat.providers == cat.endpoints
    assert cat.providers is not cat.endpoints


#: Each catalog's expected did-you-mean entry noun. Domain catalogs name
#: their entries (parameters/basins/sensors/...); ecmwf keeps the default.
ENTRY_NOUNS = [
    ("earthlens.openaq.catalog", "Catalog", "parameters"),
    ("earthlens.usgs_water.catalog", "Catalog", "parameters"),
    ("earthlens.tropycal.catalog", "Catalog", "basins"),
    ("earthlens.firms.catalog", "Catalog", "sensors"),
    ("earthlens.gdacs.catalog", "Catalog", "hazard types"),
    ("earthlens.radar.catalog", "StationCatalog", "stations"),
    ("earthlens.fdsn.catalog", "Catalog", "networks"),
    ("earthlens.overture.catalog", "Catalog", "themes"),
    ("earthlens.ecmwf.catalog", "Catalog", "datasets"),
]


@pytest.mark.parametrize("module_name, class_name, noun", ENTRY_NOUNS)
def test_did_you_mean_uses_entry_noun(module_name: str, class_name: str, noun: str):
    """The unknown-key error names the catalog's domain entry noun."""
    cat = _build(module_name, class_name)
    with pytest.raises(ValueError, match=f"Known {noun}:"):
        cat.get_dataset("definitely-not-a-key")


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_catalog_rows_are_frozen(module_name: str, class_name: str):
    """Every row model rejects attribute assignment.

    The parse cache hands the *same* row objects to every `load()`, so a row
    that allowed assignment would let one caller rewrite the catalog for the
    whole process. `frozen=True` is what makes sharing them safe.
    """
    cat = _build(module_name, class_name)
    if not cat.datasets:
        pytest.skip(f"{module_name} exposes no rows to check")
    row = cat.datasets[next(iter(cat.datasets))]
    if not hasattr(row, "model_config"):
        pytest.skip(f"{module_name} rows are not pydantic models")
    assert row.model_config.get("frozen") is True, (
        f"{module_name}.{type(row).__name__} is not frozen; the shared parse "
        "cache would let one caller mutate every other caller's catalog"
    )


def _summarised_leaf_classes() -> list[tuple[str, type]]:
    """Find every shipped `SummarisedLeaf` subclass, by class path.

    Walks the subclass tree rather than listing the adopters by hand: the
    declaration is what makes a row summarise itself, so a new adopter has to
    be checked the moment it inherits, not when someone remembers to add it
    here. `_discover_catalogs()` has already imported every backend's catalog
    module, which is what registers the subclasses.

    Test-local subclasses are filtered out — a fixture row deliberately
    declaring a bad field is exercising the degradation path, not breaking
    the contract.

    Returns:
        list[tuple[str, type]]: Sorted `(dotted class path, class)` pairs.
    """
    found: dict[str, type] = {}
    pending = [SummarisedLeaf]
    while pending:
        for sub in pending.pop().__subclasses__():
            path = f"{sub.__module__}.{sub.__qualname__}"
            if sub.__module__.startswith("earthlens.") and path not in found:
                found[path] = sub
            pending.append(sub)
    return sorted(found.items())


#: (class path, class) for every shipped catalog row that summarises itself.
SUMMARISED_LEAVES = _summarised_leaf_classes()


def test_the_cross_backend_gates_are_not_vacuous():
    """Both discovery lists are populated, so the parametrized gates run.

    Each list is built from imports that skip a backend whose SDK is absent,
    so an environment change could reduce either to nothing and every
    parametrized gate would pass by collecting no cases. Asserted here rather
    than at module scope, where a provider-free run would fail collection for
    the whole file instead of this one test.
    """
    assert len(CATALOG_BACKENDS) > 40, (
        f"only {len(CATALOG_BACKENDS)} backend catalogs importable; the "
        "cross-backend gates would pass vacuously"
    )
    assert len(SUMMARISED_LEAVES) > 100, (
        f"only {len(SUMMARISED_LEAVES)} summarising rows discovered; the "
        "summary gates would pass vacuously"
    )


def _reachable_summarised_rows(catalog: AbstractCatalog) -> list[SummarisedLeaf]:
    """Collect every `SummarisedLeaf` reachable from a built catalog.

    Walking only `catalog.datasets` misses most of them: the variable rows
    (chc, ecmwf, cmems) and the gee `Band` / `Extent` hang off a dataset row
    rather than the top level, so a top-level-only sweep never renders the
    classes the summaries were written for.

    Args:
        catalog: A built backend catalog.

    Returns:
        list[SummarisedLeaf]: Every distinct row found, outermost first.
    """
    found: list[SummarisedLeaf] = []
    seen: set[int] = set()
    pending: list[Any] = [catalog.datasets]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, SummarisedLeaf):
            found.append(current)
        if isinstance(current, BaseModel):
            pending.extend(
                getattr(current, name) for name in type(current).model_fields
            )
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set, frozenset)):
            pending.extend(current)
    return found


@pytest.mark.parametrize("path, cls", SUMMARISED_LEAVES)
def test_declared_summary_fields_exist(path: str, cls: type):
    """Every name in `_summary_fields` is a real field on the row.

    The renderer reads each declared name with a `None` default, so a typo or
    a renamed field does not raise — it silently drops that fragment and the
    summary quietly gets shorter. This is the gate that turns that into a
    failure.
    """
    missing = [
        f
        for f in getattr(cls, "_summary_fields", ())
        if f not in cls.model_fields and not hasattr(cls, f)
    ]
    assert not missing, (
        f"{path} declares {missing} in _summary_fields but carries no such "
        "field or attribute; the fragment would be silently dropped from its "
        "summary. A value computed from other fields belongs in a "
        "summary_parts() override, the way FluxableLeaf adds is_flux."
    )


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_row_summaries_are_one_line(module_name: str, class_name: str):
    """A row summary stays a single line that names the class it came from.

    Deliberately not an ASCII assertion: shipped titles legitimately carry
    `—` and `≥`, and reproducing the row's own text is the same choice
    `AbstractCatalog.__str__` makes.
    """
    rows = _reachable_summarised_rows(_build(module_name, class_name))
    if not rows:
        pytest.skip(f"{module_name} exposes no summarising rows")
    for row in rows:
        rendered = str(row)
        assert "\n" not in rendered, f"{module_name} row summary wraps: {rendered!r}"
        assert rendered.startswith(f"{type(row).__name__}("), (
            f"{module_name} row summary does not name its class: {rendered!r}"
        )


def _populated_fields(row: BaseModel) -> list[str]:
    """Return the names of `row`'s fields that carry something.

    Args:
        row: Any pydantic row.

    Returns:
        list[str]: The field names whose value is neither `None` nor an empty
        string or collection. `0` and `False` count as populated, matching
        `render_fragment`.
    """
    empty: tuple[Any, ...] = (None, "", [], {}, (), set(), frozenset())
    return [
        name
        for name in type(row).model_fields
        if not any(
            getattr(row, name, None) is blank or getattr(row, name, None) == blank
            for blank in empty
        )
    ]


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_a_row_that_holds_something_summarises_to_something(
    module_name: str, class_name: str
):
    """No row renders as a bare `ClassName()` while carrying real values.

    `SummarisedLeaf` calls an empty summary the honest answer for an empty
    row, and it is — but a row that *does* hold values and still renders as
    nothing has declared the wrong fields, which is strictly worse than the
    pydantic dump it replaced. Three backends shipped that way.
    """
    rows = _reachable_summarised_rows(_build(module_name, class_name))
    if not rows:
        pytest.skip(f"{module_name} exposes no summarising rows")
    silent = [
        (type(row).__name__, _populated_fields(row))
        for row in rows
        if str(row) == f"{type(row).__name__}()" and _populated_fields(row)
    ]
    assert not silent, (
        f"{module_name} renders {len(silent)} populated row(s) as an empty "
        f"summary, e.g. {silent[0][0]} carrying {silent[0][1]}. Declare a "
        "field that identifies the row, or compose one in summary_parts()."
    )


#: Catalog container classes, which hold rows rather than being one. Kept as an
#: escape hatch: a backend whose container genuinely subclasses `BaseModel`
#: (rather than `AbstractCatalog`) would otherwise trip the gate below.
_CONTAINER_CLASSES = {"Catalog", "StationCatalog"}


def _provider_catalog_sources() -> list[tuple[pathlib.Path, str]]:
    """Return `(path, source)` for every provider `catalog.py`.

    Resolved from this test file rather than from `earthlens.base.__file__`:
    under a non-editable wheel install the package lives in `site-packages`
    and the source tree is not beneath it, so a package-relative glob would
    match nothing and every check built on it would pass by scanning zero
    files.

    Returns:
        list[tuple[pathlib.Path, str]]: One pair per provider catalog module.
    """
    root = pathlib.Path(__file__).resolve().parents[3] / "providers"
    return [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(root.glob("*/src/earthlens/*/catalog.py"))
    ]


def _basemodel_aliases(tree: ast.Module) -> set[str]:
    """Return every name in `tree` that refers to `pydantic.BaseModel`.

    `from pydantic import BaseModel as Model` and a bare `import pydantic`
    both hide the class from a plain name match, which is how a row could
    quietly rejoin `BaseModel` without tripping the gate.

    Args:
        tree: The parsed module.

    Returns:
        set[str]: The local names bound to `BaseModel`, plus the dotted
        spellings reachable through an imported `pydantic` module.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pydantic":
            names |= {a.asname or a.name for a in node.names if a.name == "BaseModel"}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pydantic":
                    names.add(f"{alias.asname or 'pydantic'}.BaseModel")
    return names


def _base_names(node: ast.ClassDef) -> set[str]:
    """Return the spellings of `node`'s bases, plain and dotted.

    Args:
        node: A class definition.

    Returns:
        set[str]: `"BaseModel"` for a bare base, `"pydantic.BaseModel"` for an
        attribute base, ignoring subscripted generics like
        `AbstractCatalog[Row]`.
    """
    spellings = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            spellings.add(base.id)
        elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            spellings.add(f"{base.value.id}.{base.attr}")
    return spellings


def _row_classes_still_on_basemodel() -> list[str]:
    """Find provider catalog row classes that do not summarise themselves.

    Static rather than runtime: a class only registers as a `SummarisedLeaf`
    subclass once its module is imported, and a row that no shipped catalog
    populates would never be instantiated at all. Reading the source catches
    both.

    Walks every class in the module, not only the top-level ones, and resolves
    `BaseModel` through its import aliases, so the spellings a plain name match
    would miss are still seen.

    Returns:
        list[str]: `"<backend>.<ClassName>"` for each row still on
        `BaseModel`, sorted.
    """
    found = []
    for path, source in _provider_catalog_sources():
        tree = ast.parse(source)
        aliases = _basemodel_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name in _CONTAINER_CLASSES:
                continue
            if _base_names(node) & aliases:
                found.append(f"{path.parent.name}.{node.name}")
    return sorted(found)


def test_the_catalog_scan_actually_reads_the_sources():
    """Guard the guard: the gate below is worthless if it scans nothing."""
    sources = _provider_catalog_sources()
    assert len(sources) > 40, (
        f"only {len(sources)} provider catalog modules found; "
        "test_every_catalog_row_summarises_itself would pass vacuously"
    )


def test_every_catalog_row_summarises_itself():
    """No provider catalog row is left on `BaseModel`.

    A row that does not inherit `SummarisedLeaf` answers `print(row)` with
    pydantic's field dump, which is the inconsistency #1185 existed to close.
    Guarding it statically stops the next backend reintroducing it one class
    at a time.
    """
    stragglers = _row_classes_still_on_basemodel()
    assert not stragglers, (
        f"{len(stragglers)} catalog row class(es) still subclass BaseModel and "
        f"will print pydantic's full field dump: {stragglers}. Inherit "
        "SummarisedLeaf and declare _summary_fields, or add the class to "
        "_CONTAINER_CLASSES if it holds rows rather than being one."
    )


def _classes_and_aliases(source: str) -> tuple[list[ast.ClassDef], set[str]]:
    """Parse `source` and return its class definitions and its BaseModel aliases.

    Args:
        source: Python source text.

    Returns:
        tuple[list[ast.ClassDef], set[str]]: Every class in the tree, in
        walk order, and the local names that refer to `pydantic.BaseModel`.
    """
    tree = ast.parse(source)
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    return classes, _basemodel_aliases(tree)


@pytest.mark.parametrize(
    "declaration, imports",
    [
        ("class Row(BaseModel):", "from pydantic import BaseModel"),
        ("class Row(pydantic.BaseModel):", "import pydantic"),
        ("class Row(Model):", "from pydantic import BaseModel as Model"),
    ],
    ids=["bare", "dotted", "aliased"],
)
def test_the_gate_sees_every_basemodel_spelling(declaration: str, imports: str):
    """A row rejoining BaseModel is caught however the base is spelled."""
    classes, aliases = _classes_and_aliases(
        f"{imports}\n{declaration}\n    x: str = ''\n"
    )
    assert _base_names(classes[0]) & aliases, (
        f"{declaration!r} was not recognised as a BaseModel subclass"
    )


def test_the_gate_ignores_a_summarised_row():
    """A row that already summarises itself is left alone."""
    classes, aliases = _classes_and_aliases(
        "from earthlens.base import SummarisedLeaf\n"
        "class Row(SummarisedLeaf):\n"
        "    x: str = ''\n"
    )
    assert not _base_names(classes[0]) & aliases


def test_the_gate_sees_a_class_nested_in_a_function():
    """The scan walks the whole tree, so a row hidden in a factory is seen."""
    classes, aliases = _classes_and_aliases(
        "from pydantic import BaseModel\n"
        "def factory():\n"
        "    class Hidden(BaseModel):\n"
        "        x: str = ''\n"
        "    return Hidden\n"
    )
    assert [node.name for node in classes] == ["Hidden"]
    assert _base_names(classes[0]) & aliases
