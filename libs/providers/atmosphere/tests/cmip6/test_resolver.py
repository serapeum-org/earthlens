"""Unit tests for the CMIP6 facet -> zstore resolver (offline fixture frame)."""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.cmip6 import ResolvedStore, StoreResolver
from earthlens.cmip6.resolver import default_cache_path

pytestmark = [pytest.mark.cmip6, pytest.mark.unit]


def test_resolve_single_store_latest_version(resolver):
    """A fully-pinned facet tuple resolves to the newest store version."""
    out = resolver.resolve(
        source_id="CanESM5",
        experiment_id="ssp585",
        variable_id="tas",
        table_id="Amon",
        member_id="r1i1p1f1",
        grid_label="gn",
    )
    assert len(out) == 1
    assert out[0].version == "20190429"
    assert out[0].zstore.endswith("gn/v20190429/")


def test_resolve_fans_out_over_members(resolver):
    """Leaving member_id unset fans out to one store per member (latest each)."""
    out = resolver.resolve(
        source_id="CanESM5",
        experiment_id="ssp585",
        variable_id="tas",
        table_id="Amon",
        grid_label="gn",
    )
    members = sorted(s.member_id for s in out)
    assert members == ["r1i1p1f1", "r2i1p1f1"]


def test_resolve_explicit_version(resolver):
    """An explicit version selects that publication, not the latest."""
    out = resolver.resolve(
        source_id="CanESM5",
        experiment_id="ssp585",
        variable_id="tas",
        table_id="Amon",
        member_id="r1i1p1f1",
        version="20190101",
    )
    assert [s.version for s in out] == ["20190101"]


def test_resolve_activity_and_grid_filters(resolver):
    """activity_id and grid_label narrow the match set."""
    out = resolver.resolve(
        source_id="GFDL-ESM4",
        experiment_id="ssp585",
        variable_id="pr",
        table_id="day",
        activity_id="ScenarioMIP",
        grid_label="gr1",
    )
    assert len(out) == 1
    assert out[0].source_id == "GFDL-ESM4"


def test_resolve_miss_names_facet_and_values(resolver):
    """A facet with no match raises listing the values that were available."""
    with pytest.raises(ValueError, match="experiment_id='ssp999'"):
        resolver.resolve(
            source_id="CanESM5",
            experiment_id="ssp999",
            variable_id="tas",
            table_id="Amon",
        )


def test_resolve_miss_describes_prior_facets(resolver):
    """The miss message reports the facets pinned before the failing one."""
    with pytest.raises(ValueError, match="source_id=CanESM5"):
        resolver.resolve(
            source_id="CanESM5",
            experiment_id="ssp585",
            variable_id="tas",
            table_id="Emon",
        )


def test_resolve_miss_did_you_mean(resolver):
    """A near-miss facet value gets a concise did-you-mean suggestion."""
    with pytest.raises(ValueError, match="Did you mean 'CanESM5'"):
        resolver.resolve(
            source_id="CanESM",
            experiment_id="ssp585",
            variable_id="tas",
            table_id="Amon",
        )


def test_resolve_miss_caps_long_value_list():
    """A miss on a high-cardinality facet truncates the listed values."""
    rows = [
        {
            "source_id": f"MODEL-{i:03d}",
            "experiment_id": "ssp585",
            "variable_id": "tas",
            "table_id": "Amon",
            "grid_label": "gn",
            "version": 1,
            "zstore": f"gs://cmip6/{i}/",
        }
        for i in range(40)
    ]
    resolver = StoreResolver("http://x/y.csv", ["source_id"], frame=pd.DataFrame(rows))
    with pytest.raises(ValueError, match=r"\+20 more"):
        resolver.resolve(
            source_id="NOPE",
            experiment_id="ssp585",
            variable_id="tas",
            table_id="Amon",
        )


def test_resolve_explicit_version_miss(resolver):
    """An explicit version with no match lists the available versions."""
    with pytest.raises(ValueError, match="available versions"):
        resolver.resolve(
            source_id="CanESM5",
            experiment_id="ssp585",
            variable_id="tas",
            table_id="Amon",
            member_id="r1i1p1f1",
            version="20200101",
        )


def test_resolved_store_slug(resolver):
    """The resolved store's slug joins its identifying facets."""
    store = resolver.resolve(
        source_id="CanESM5",
        experiment_id="ssp585",
        variable_id="tas",
        table_id="Amon",
        member_id="r1i1p1f1",
        grid_label="gn",
    )[0]
    assert store.slug == "CanESM5_ssp585_tas_Amon_r1i1p1f1_gn"


def test_slug_strips_path_separators():
    """A member id carrying a slash is sanitised in the slug."""
    store = ResolvedStore(
        zstore="gs://cmip6/x/",
        source_id="M",
        experiment_id="e",
        variable_id="v",
        table_id="Amon",
        member_id="r1/i1",
        grid_label="gn",
        version="1",
    )
    assert "/" not in store.slug


def test_frame_property_uses_injected_frame(store_frame):
    """The injected frame is returned verbatim without any download."""
    resolver = StoreResolver("http://x/y.csv", ["source_id"], frame=store_frame)
    assert resolver.frame is store_frame


def test_frame_loads_from_local_cache(tmp_path, store_frame):
    """A present cache file is read instead of downloading."""
    cache = tmp_path / "stores.csv"
    store_frame.to_csv(cache, index=False)
    resolver = StoreResolver(
        "http://x/y.csv", list(store_frame.columns), cache_path=cache
    )
    assert len(resolver.frame) == len(store_frame)


def test_default_cache_path_honours_env(monkeypatch, tmp_path):
    """EARTHLENS_CACHE relocates the default CSV cache path."""
    from earthlens.config import set_cache_dir

    set_cache_dir(None)  # the test-isolation override outranks the env var
    monkeypatch.setenv("EARTHLENS_CACHE", str(tmp_path))
    path = default_cache_path()
    assert path == tmp_path.resolve() / "cmip6" / "pangeo-cmip6.csv"


def test_default_cache_path_without_env(monkeypatch):
    """Without EARTHLENS_CACHE the cache falls under the shared cache directory."""
    from earthlens.config import set_cache_dir

    set_cache_dir(None)  # the test-isolation override outranks the env var
    monkeypatch.delenv("EARTHLENS_CACHE", raising=False)
    path = default_cache_path()
    assert path.parts[-2:] == ("cmip6", "pangeo-cmip6.csv")


def test_ensure_csv_downloads_when_absent(monkeypatch, tmp_path, store_frame):
    """A missing cache triggers a streamed download that is then cached."""
    cache = tmp_path / "stores.csv"
    payload = store_frame.to_csv(index=False).encode()

    class _Resp:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield payload

        def close(self):
            return None

    monkeypatch.setattr("requests.get", lambda url, **kwargs: _Resp())
    resolver = StoreResolver(
        "http://x/y.csv", list(store_frame.columns), cache_path=cache
    )
    assert len(resolver.frame) == len(store_frame)
    assert cache.exists()


def test_select_version_no_version_column():
    """A frame without a version column is returned unreduced."""
    frame = pd.DataFrame([{"source_id": "M", "zstore": "gs://cmip6/a/"}])
    resolver = StoreResolver("http://x/y.csv", ["source_id"], frame=frame)
    out = resolver.resolve(
        source_id="M",
        experiment_id="e",
        variable_id="v",
        table_id="Amon",
    )
    assert len(out) == 1


def test_select_version_no_identity_columns():
    """A frame with only version + zstore reduces by version with no dedup."""
    frame = pd.DataFrame(
        [
            {"version": 1, "zstore": "gs://cmip6/a/"},
            {"version": 2, "zstore": "gs://cmip6/b/"},
        ]
    )
    resolver = StoreResolver("http://x/y.csv", ["version"], frame=frame)
    out = resolver.resolve(
        source_id="M",
        experiment_id="e",
        variable_id="v",
        table_id="Amon",
    )
    assert {s.version for s in out} == {"1", "2"}


def test_describe_without_matching_up_to():
    """_describe lists all pinned facets when up_to is not among them."""
    summary = StoreResolver._describe({"source_id": "M"}, "not_a_facet")
    assert "source_id=M" in summary


def test_ensure_csv_empty_download_raises(monkeypatch, tmp_path):
    """An empty downloaded body raises rather than caching a useless file."""
    cache = tmp_path / "stores.csv"

    class _Resp:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            return iter(())

        def close(self):
            return None

    monkeypatch.setattr("requests.get", lambda url, **kwargs: _Resp())
    resolver = StoreResolver("http://x/y.csv", ["source_id"], cache_path=cache)
    with pytest.raises(RuntimeError, match="empty CSV"):
        _ = resolver.frame
    assert not cache.exists()
