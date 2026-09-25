"""The NCERT importer must be able to file everything under a folder path.

The importer used to create ``Class_XII`` at the ROOT. Prod keeps its books under
``India > ncert``, so ``--parent-path India/ncert`` has to create/reuse that chain
first and hang the Class/Subject folders off the deepest segment. These tests pin
the resolution rules: an empty path stays at the root (old behaviour), each segment
is slugged like a Class folder, and existing folders are reused rather than
duplicated.
"""

from __future__ import annotations

import pytest

from scripts.ncert_bulk_import import (
    ApiClient,
    ScriptError,
    ensure_folder,
    resolve_parent_path,
)


class FakeFolderClient(ApiClient):
    """Stands in for ApiClient: an in-memory folder tree keyed by parent id."""

    def __init__(self, existing: dict | None = None, post_fails: bool = False):
        super().__init__("http://edu-viz.test", None)
        self.tree: dict = {k: [dict(f) for f in v] for k, v in (existing or {}).items()}
        self.created: list[tuple[str, str | None, str]] = []
        self.post_fails = post_fails
        self._seq = 0

    def get_json(self, path: str, params: dict | None = None):
        if path == "/api/v1/folders":
            return list(self.tree.get(None, []))
        # /api/v1/folders/<parent_id>/subfolders
        parent_id = path.split("/")[4]
        return list(self.tree.get(parent_id, []))

    def post_json(self, path: str, payload: dict):
        if self.post_fails:
            raise ScriptError("boom")
        self._seq += 1
        folder_id = f"id-{self._seq}"
        self.created.append((payload["name"], payload["parent_id"], folder_id))
        self.tree.setdefault(payload["parent_id"], []).append(
            {"id": folder_id, "name": payload["name"]}
        )
        return {"id": folder_id}


def test_empty_parent_path_stays_at_the_root_and_creates_nothing():
    client = FakeFolderClient()

    assert resolve_parent_path(client, "") is None
    assert resolve_parent_path(client, "   ") is None
    assert client.created == []


def test_parent_path_creates_each_segment_under_the_previous_one():
    client = FakeFolderClient()

    deepest = resolve_parent_path(client, "India/ncert")

    assert client.created == [("India", None, "id-1"), ("ncert", "id-1", "id-2")]
    assert deepest == "id-2"


def test_parent_path_reuses_existing_folders_without_posting():
    client = FakeFolderClient(
        existing={
            None: [{"id": "india", "name": "India"}],
            "india": [{"id": "ncert", "name": "ncert"}],
        }
    )

    assert resolve_parent_path(client, "India/ncert") == "ncert"
    assert client.created == []


def test_parent_path_slugs_each_segment_like_a_class_folder():
    """Folder names must match ^[a-zA-Z0-9_]+$, so "Class I" becomes Class_I."""
    client = FakeFolderClient()

    deepest = resolve_parent_path(client, "Class I/ncert")

    assert [name for name, _parent, _id in client.created] == ["Class_I", "ncert"]
    assert deepest == "id-2"


def test_parent_path_ignores_empty_segments_and_whitespace():
    client = FakeFolderClient()

    deepest = resolve_parent_path(client, " /India//ncert/ ")

    assert [name for name, _parent, _id in client.created] == ["India", "ncert"]
    assert deepest == "id-2"


def test_class_folder_hangs_off_the_resolved_parent():
    """The class folder's parent must be the deepest segment, not the root."""
    client = FakeFolderClient(existing={None: [{"id": "india", "name": "India"}]})

    parent_id = resolve_parent_path(client, "India/ncert")
    class_folder = ensure_folder(client, "Class I", parent_id)
    subject_folder = ensure_folder(client, "English", class_folder)

    # India already existed and was reused; everything below it is new.
    assert parent_id == "id-1"
    assert class_folder == "id-2"
    assert subject_folder == "id-3"
    assert client.created == [
        ("ncert", "india", "id-1"),
        ("Class_I", "id-1", "id-2"),
        ("English", "id-2", "id-3"),
    ]


def test_missing_parent_path_raises_rather_than_silently_using_the_root():
    """A dead API must fail the import, not file books at the root by accident."""
    client = FakeFolderClient(post_fails=True)

    with pytest.raises(ScriptError):
        resolve_parent_path(client, "India/ncert")
