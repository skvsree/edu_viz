"""The bulk path must not hold a chapter's PDF, or its parse, in memory.

Measured 2026-10-07 on a 17 MiB chapter: the container's RSS grew ~3.6x the file
size and stayed there, because the worker read the whole PDF into bytes, handed
pypdf a second in-memory copy, and the freed arenas were never returned to the
OS. These tests pin the three fixes - stream the stored object to disk, parse
from a file handle, and release the memory once the file is done.
"""

from __future__ import annotations

import io
import os

import pypdf
import pytest

from app.services.job_worker import (
    extract_text_from_pdf,
    materialise_upload_file,
    release_memory,
)
from app.services.storage import LocalStorage


def _tiny_pdf() -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_extract_text_accepts_bytes_stream_and_path(tmp_path):
    payload = _tiny_pdf()
    assert extract_text_from_pdf(payload) == ""

    path = tmp_path / "chapter.pdf"
    path.write_bytes(payload)
    assert extract_text_from_pdf(str(path)) == ""
    assert extract_text_from_pdf(path) == ""

    handle = open(path, "rb")
    assert extract_text_from_pdf(handle) == ""
    # The worker owns the handle it opens and closes it itself, so a handle
    # handed in from outside must come back untouched.
    assert handle.closed is False
    handle.close()


def test_extract_text_returns_empty_for_an_unreadable_source():
    assert extract_text_from_pdf(b"not a pdf") == ""


def test_materialise_upload_file_streams_the_stored_object(tmp_path):
    storage = LocalStorage(base_dir=tmp_path / "media")
    payload = b"%PDF-1.4\n" + os.urandom(4096)
    storage.save_bytes(key="bulk/ch1.pdf", data=payload)

    path = materialise_upload_file(storage, "bulk/ch1.pdf")
    try:
        assert path.exists()
        assert path.read_bytes() == payload
    finally:
        path.unlink(missing_ok=True)
    assert not path.exists()


def test_materialise_upload_file_surfaces_a_missing_key(tmp_path):
    storage = LocalStorage(base_dir=tmp_path / "media")
    with pytest.raises(FileNotFoundError):
        materialise_upload_file(storage, "bulk/nope.pdf")


def test_download_to_file_streams_in_chunks(tmp_path):
    """A download must not depend on having the whole object in memory."""
    storage = LocalStorage(base_dir=tmp_path / "media")
    payload = b"x" * 5000
    storage.save_bytes(key="bulk/big.pdf", data=payload)

    out = tmp_path / "out.bin"
    with open(out, "wb") as dest:
        content_type = storage.download_to_file(key="bulk/big.pdf", dest=dest, chunk_size=512)
    assert out.read_bytes() == payload
    assert content_type in (None, "application/pdf")


def test_release_memory_is_safe():
    assert release_memory() is None


def test_pool_settings_read_from_the_environment(monkeypatch):
    from app.core import db

    monkeypatch.setenv("DB_POOL_SIZE_PROBE", "42")
    assert db._int_env("DB_POOL_SIZE_PROBE", 7) == 42
    monkeypatch.setenv("DB_POOL_SIZE_PROBE", "junk")
    assert db._int_env("DB_POOL_SIZE_PROBE", 7) == 7
    assert db._int_env("DB_POOL_SIZE_ABSENT", 7) == 7

    if hasattr(db.engine.pool, "size"):
        assert db.engine.pool.size() == db.POOL_SIZE
    assert db.MAX_OVERFLOW >= db.POOL_SIZE
    assert db.POOL_RECYCLE > 0
