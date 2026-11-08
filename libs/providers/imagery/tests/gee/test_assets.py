"""Tests for the `AssetManager` control-plane over `ee.data`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee import assets as assets_module  # noqa: E402
from earthlens.gee import jobs as jobs_module  # noqa: E402
from earthlens.gee.assets import AssetInfo, AssetManager  # noqa: E402
from earthlens.gee.jobs import TaskInfo  # noqa: E402

_OPERATION = {
    "name": "projects/p/operations/task-9",
    "metadata": {
        "state": "PENDING",
        "type": "INGEST_IMAGE",
        "createTime": "2024-01-01T00:00:00Z",
        "updateTime": "2024-01-01T00:00:00Z",
        "description": "ingest",
    },
    "done": False,
}


class _FakeEeData:
    """Records every `ee.data` asset call and answers with canned payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.assets_response: dict = {
            "assets": [
                {"id": "projects/p/assets/f/a", "type": "IMAGE", "sizeBytes": "1024"},
                {"name": "projects/p/assets/f/b", "type": "TABLE"},
            ]
        }

    def _record(self, name: str, *args: object, **kwargs: object) -> None:
        self.calls.append((name, args, kwargs))

    def createFolder(self, path):  # noqa: N802 - EE method name
        self._record("createFolder", path)
        return {"id": path, "type": "FOLDER"}

    def createAsset(self, value, path=None, properties=None):  # noqa: N802
        self._record("createAsset", value, path, properties)
        return {"id": path, "type": value["type"]}

    def listAssets(self, params):  # noqa: N802
        self._record("listAssets", params)
        return self.assets_response

    def getAsset(self, asset_id):  # noqa: N802
        self._record("getAsset", asset_id)
        return {"id": asset_id, "type": "IMAGE", "sizeBytes": "2048"}

    def deleteAsset(self, asset_id):  # noqa: N802
        self._record("deleteAsset", asset_id)

    def copyAsset(self, source, destination, allow_overwrite=False):  # noqa: N802
        self._record("copyAsset", source, destination, allow_overwrite)

    def renameAsset(self, source, destination):  # noqa: N802
        self._record("renameAsset", source, destination)

    def setAssetProperties(self, asset_id, properties):  # noqa: N802
        self._record("setAssetProperties", asset_id, properties)

    def getAssetAcl(self, asset_id):  # noqa: N802
        self._record("getAssetAcl", asset_id)
        return {
            "owners": ["o@x"],
            "writers": [],
            "readers": [],
            "all_users_can_read": False,
        }

    def setAssetAcl(self, asset_id, acl):  # noqa: N802
        self._record("setAssetAcl", asset_id, acl)

    def newTaskId(self, count=1):  # noqa: N802
        self._record("newTaskId", count)
        return ["minted-id"]

    def startIngestion(self, request_id, params, allow_overwrite=False):  # noqa: N802
        self._record("startIngestion", request_id, params, allow_overwrite)
        return {
            "id": request_id,
            "name": "projects/p/operations/task-9",
            "started": "OK",
        }

    def startTableIngestion(self, request_id, params, allow_overwrite=False):  # noqa: N802
        self._record("startTableIngestion", request_id, params, allow_overwrite)
        return {
            "id": request_id,
            "name": "projects/p/operations/task-9",
            "started": "OK",
        }


@pytest.fixture
def fake_data(monkeypatch):
    """Patch the assets module's `ee` and the jobs operation lookup."""
    fake = _FakeEeData()
    monkeypatch.setattr(assets_module, "ee", SimpleNamespace(data=fake))
    monkeypatch.setattr(jobs_module.ee.data, "getOperation", lambda name: _OPERATION)
    return fake


class TestAssetManagerConfig:
    """The manager is a frozen value object holding only the project."""

    def test_defaults(self):
        """A bare manager carries no explicit project."""
        assert AssetManager().project is None

    def test_it_is_frozen(self):
        """The project cannot drift after construction."""
        manager = AssetManager(project="p")
        with pytest.raises(ValidationError):
            manager.project = "other"

    def test_a_typo_is_refused(self):
        """An unknown keyword raises instead of silently no-op'ing."""
        with pytest.raises(ValidationError):
            AssetManager(projects="p")

    def test_root_builds_the_project_asset_root(self):
        """An explicit project resolves to its `projects/<p>/assets` root."""
        assert AssetManager(project="my-project").root == "projects/my-project/assets"


class TestAssetInfo:
    """Raw `ee.data` dicts normalise into one-line rows."""

    def test_get_asset_parses_the_row(self, fake_data):
        """`getAsset`'s dict becomes an AssetInfo with a parsed size."""
        info = AssetManager(project="p").get_asset("projects/p/assets/x")
        assert fake_data.calls == [("getAsset", ("projects/p/assets/x",), {})]
        assert info == AssetInfo(
            id="projects/p/assets/x", type="IMAGE", size_bytes=2048
        )

    def test_it_prints_as_one_line(self):
        """The row summarises instead of dumping every field."""
        row = AssetInfo(id="projects/p/assets/x", type="FOLDER")
        assert str(row) == "AssetInfo(projects/p/assets/x, FOLDER)"

    def test_a_payload_with_no_handle_is_reported(self):
        """Neither `id` nor `name` leaves nothing to address, so it raises."""
        with pytest.raises(ValueError, match="neither 'id' nor 'name'"):
            assets_module._to_asset_info({"type": "FOLDER"})

    def test_the_refusal_names_the_keys_it_got(self):
        """The message carries the payload's keys, as `_submitted_task`'s does."""
        with pytest.raises(ValueError, match=r"\['size[Bb]ytes', 'type'\]"):
            assets_module._to_asset_info({"type": "FOLDER", "sizeBytes": "1"})

    @pytest.mark.parametrize("key", ["id", "name"])
    def test_either_handle_is_enough(self, key):
        """Both spellings normalise onto `id`, as before."""
        assert assets_module._to_asset_info({key: "projects/p/assets/x"}).id == (
            "projects/p/assets/x"
        )

    def test_an_empty_handle_is_reported_too(self):
        """An empty string is not an asset id either."""
        with pytest.raises(ValueError, match="neither 'id' nor 'name'"):
            assets_module._to_asset_info({"id": "", "type": "FOLDER"})

    def test_it_is_frozen(self):
        """A listing row cannot be edited in place."""
        row = AssetInfo(id="projects/p/assets/x")
        with pytest.raises(ValidationError):
            row.type = "IMAGE"


class TestCreate:
    """Folder and asset creation dispatch to `ee.data.create*`."""

    def test_create_folder(self, fake_data):
        """`create_folder` calls `ee.data.createFolder` with the path."""
        info = AssetManager(project="p").create_folder("projects/p/assets/f")
        assert fake_data.calls == [("createFolder", ("projects/p/assets/f",), {})]
        assert (info.id, info.type) == ("projects/p/assets/f", "FOLDER")

    def test_create_asset_defaults_to_an_image_collection(self, fake_data):
        """`create_asset` wraps the type the way `ee.data.createAsset` wants."""
        info = AssetManager().create_asset("projects/p/assets/c")
        assert fake_data.calls == [
            (
                "createAsset",
                ({"type": "IMAGE_COLLECTION"}, "projects/p/assets/c", None),
                {},
            )
        ]
        assert info.type == "IMAGE_COLLECTION"

    def test_create_asset_passes_type_and_properties(self, fake_data):
        """An explicit type and properties reach the SDK verbatim."""
        AssetManager().create_asset(
            "projects/p/assets/f", asset_type="FOLDER", properties={"k": "v"}
        )
        assert fake_data.calls == [
            ("createAsset", ({"type": "FOLDER"}, "projects/p/assets/f", {"k": "v"}), {})
        ]


class TestListing:
    """Listings come back as parsed rows, not raw dicts."""

    def test_list_assets_parses_each_row(self, fake_data):
        """Both the `id` and the `name` spellings land in AssetInfo rows."""
        rows = AssetManager(project="p").list_assets("projects/p/assets/f")
        assert fake_data.calls == [
            ("listAssets", ({"parent": "projects/p/assets/f"},), {})
        ]
        assert rows == [
            AssetInfo(id="projects/p/assets/f/a", type="IMAGE", size_bytes=1024),
            AssetInfo(id="projects/p/assets/f/b", type="TABLE"),
        ]

    def test_list_assets_defaults_to_the_project_root(self, fake_data, monkeypatch):
        """With no parent, the manager lists its resolved project's root."""
        monkeypatch.setattr(assets_module, "_resolve_project", lambda p: "resolved")
        AssetManager().list_assets()
        assert fake_data.calls == [
            ("listAssets", ({"parent": "projects/resolved/assets"},), {})
        ]

    def test_an_empty_listing_is_an_empty_list(self, fake_data):
        """A folder with nothing in it answers `[]`, not a KeyError."""
        fake_data.assets_response = {}
        assert AssetManager(project="p").list_assets("projects/p/assets/f") == []


class TestLifecycle:
    """Delete, copy, rename and property updates dispatch with the right args."""

    def test_delete(self, fake_data):
        """`delete` calls `ee.data.deleteAsset` with the asset id."""
        AssetManager(project="p").delete("projects/p/assets/x")
        assert fake_data.calls == [("deleteAsset", ("projects/p/assets/x",), {})]

    def test_copy_defaults_to_no_overwrite(self, fake_data):
        """`copy_asset` forwards source, destination and the overwrite flag."""
        AssetManager().copy_asset("projects/p/assets/x", "projects/p/assets/y")
        assert fake_data.calls == [
            ("copyAsset", ("projects/p/assets/x", "projects/p/assets/y", False), {})
        ]

    def test_copy_can_overwrite(self, fake_data):
        """`overwrite=True` reaches `copyAsset` as its third argument."""
        AssetManager().copy_asset("a", "b", overwrite=True)
        assert fake_data.calls == [("copyAsset", ("a", "b", True), {})]

    def test_rename(self, fake_data):
        """`rename` calls `ee.data.renameAsset` with both ids."""
        AssetManager().rename("projects/p/assets/x", "projects/p/assets/y")
        assert fake_data.calls == [
            ("renameAsset", ("projects/p/assets/x", "projects/p/assets/y"), {})
        ]

    def test_update_properties(self, fake_data):
        """`update_properties` calls `ee.data.setAssetProperties`."""
        AssetManager().update_properties("projects/p/assets/x", {"k": 1})
        assert fake_data.calls == [
            ("setAssetProperties", ("projects/p/assets/x", {"k": 1}), {})
        ]


class TestAcl:
    """ACLs round-trip through `getAssetAcl` / `setAssetAcl`."""

    def test_get_acl(self, fake_data):
        """`get_acl` returns the SDK's ACL dict."""
        acl = AssetManager().get_acl("projects/p/assets/x")
        assert fake_data.calls == [("getAssetAcl", ("projects/p/assets/x",), {})]
        assert acl["owners"] == ["o@x"]

    def test_set_acl(self, fake_data):
        """`set_acl` forwards the update dict verbatim."""
        AssetManager().set_acl("projects/p/assets/x", {"all_users_can_read": True})
        assert fake_data.calls == [
            ("setAssetAcl", ("projects/p/assets/x", {"all_users_can_read": True}), {})
        ]


class TestIngestion:
    """Ingestion submits through `ee.data` and tracks through the jobs machinery."""

    def test_start_ingestion_tracks_as_a_taskinfo(self, fake_data):
        """The submission's operation name resolves to a normalised TaskInfo."""
        params = {"name": "projects/p/assets/x", "tilesets": []}
        info = AssetManager(project="p").start_ingestion(params, request_id="rid-1")
        assert ("startIngestion", ("rid-1", params, False), {}) in fake_data.calls
        assert isinstance(info, TaskInfo)
        assert (info.id, info.state, info.task_type) == (
            "task-9",
            "READY",
            "INGEST_IMAGE",
        )

    def test_start_ingestion_mints_a_request_id(self, fake_data):
        """Without a request id, one comes from `ee.data.newTaskId`."""
        AssetManager(project="p").start_ingestion({"name": "x"})
        assert ("newTaskId", (1,), {}) in fake_data.calls
        assert (
            "startIngestion",
            ("minted-id", {"name": "x"}, False),
            {},
        ) in fake_data.calls

    def test_start_ingestion_can_overwrite(self, fake_data):
        """`allow_overwrite=True` reaches the SDK."""
        AssetManager(project="p").start_ingestion(
            {"name": "x"}, request_id="r", allow_overwrite=True
        )
        assert ("startIngestion", ("r", {"name": "x"}, True), {}) in fake_data.calls

    def test_start_table_ingestion(self, fake_data):
        """The table variant dispatches to `startTableIngestion`."""
        params = {"name": "projects/p/assets/t", "sources": []}
        info = AssetManager(project="p").start_table_ingestion(
            params, request_id="rid-2"
        )
        assert ("startTableIngestion", ("rid-2", params, False), {}) in fake_data.calls
        assert (info.id, info.task_type) == ("task-9", "INGEST_IMAGE")

    def test_a_nameless_submission_falls_back_to_its_id(self, fake_data, monkeypatch):
        """A response without an operation name tracks by bare task id."""
        monkeypatch.setattr(
            fake_data,
            "startIngestion",
            lambda rid, params, overwrite=False: {"id": "task-9", "started": "OK"},
        )
        info = AssetManager(project="p").start_ingestion({"name": "x"}, request_id="r")
        assert info.id == "task-9"

    def test_a_handleless_submission_is_named(self, fake_data, monkeypatch):
        """Neither key present reports the keys, not a bare `KeyError`."""
        monkeypatch.setattr(
            fake_data,
            "startIngestion",
            lambda rid, params, overwrite=False: {"started": "OK"},
        )
        manager = AssetManager(project="p")
        with pytest.raises(ValueError, match="neither 'name' nor 'id'"):
            manager.start_ingestion({"name": "x"}, request_id="r")


#: Every `ee.data` name `AssetManager` calls, with the parameter names the real
#: SDK takes (read off `inspect.signature` on earthengine-api, not guessed —
#: note `getAsset(asset_id)` against `deleteAsset(assetId)`, and the ingestion
#: pair's snake_case). The fake above defines its own stand-ins, which pins the
#: call graph but not the SDK surface: a rename or a signature change upstream
#: would leave the rest of this module green. `TestEeDataSurface` closes that
#: gap against the real module, the way `test_video_sink.py` /
#: `test_table_sink.py` pin `Task.Type` and `test_workload.py` drives the real
#: `setWorkloadTag`.
_EE_DATA_SURFACE: dict[str, tuple[str, ...]] = {
    "newTaskId": ("count",),
    "createFolder": ("path",),
    "createAsset": ("value", "path", "properties"),
    "listAssets": ("params",),
    "getAsset": ("asset_id",),
    "deleteAsset": ("assetId",),
    "copyAsset": ("sourceId", "destinationId", "allowOverwrite"),
    "renameAsset": ("sourceId", "destinationId"),
    "setAssetProperties": ("assetId", "properties"),
    "getAssetAcl": ("assetId",),
    "setAssetAcl": ("assetId", "aclUpdate"),
    "startIngestion": ("request_id", "params", "allow_overwrite"),
    "startTableIngestion": ("request_id", "params", "allow_overwrite"),
}


class TestEeDataSurface:
    """L9: the faked `ee.data` names exist on the real SDK, with these params."""

    def test_every_faked_name_exists_on_the_sdk(self):
        """A rename upstream fails here instead of passing against the fake."""
        import ee

        missing = [name for name in _EE_DATA_SURFACE if not hasattr(ee.data, name)]
        assert missing == []

    @pytest.mark.parametrize("name", sorted(_EE_DATA_SURFACE))
    def test_each_signature_still_takes_the_parameters_passed(self, name):
        """The parameters `AssetManager` relies on are still on the signature."""
        import inspect

        import ee

        parameters = inspect.signature(getattr(ee.data, name)).parameters
        assert set(_EE_DATA_SURFACE[name]) <= set(parameters)

    def test_the_map_covers_exactly_the_names_the_fake_stands_in_for(self):
        """The map and the fake cannot drift apart silently."""
        faked = {
            name
            for name, value in vars(_FakeEeData).items()
            if callable(value) and not name.startswith("_")
        }
        assert faked == set(_EE_DATA_SURFACE)
