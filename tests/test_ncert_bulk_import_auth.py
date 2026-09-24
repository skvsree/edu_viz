"""The NCERT importer must be able to authenticate with the bulk-import key.

Before this, the script could only use a browser session cookie (or mint one),
because the key covered just ``/api/v1/import/*``. These tests pin the key
plumbing: the header is sent, and the key can come from a flag, a file, or the
environment.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from scripts.ncert_bulk_import import ApiClient, ScriptError, resolve_api_key


def _args(**overrides) -> Namespace:
    base = {"api_key": None, "api_key_file": None}
    base.update(overrides)
    return Namespace(**base)


def test_api_key_from_the_flag():
    assert resolve_api_key(_args(api_key="  from-flag  ")) == "from-flag"


def test_api_key_from_a_file(tmp_path: Path):
    key_file = tmp_path / "key.txt"
    key_file.write_text("from-file\n", encoding="utf-8")

    assert resolve_api_key(_args(api_key_file=str(key_file))) == "from-file"


def test_api_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("BULK_IMPORT_API_KEY", "from-env")

    assert resolve_api_key(_args()) == "from-env"


def test_flag_beats_file_and_environment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BULK_IMPORT_API_KEY", "from-env")
    key_file = tmp_path / "key.txt"
    key_file.write_text("from-file\n", encoding="utf-8")

    assert resolve_api_key(_args(api_key="from-flag", api_key_file=str(key_file))) == "from-flag"


def test_no_key_anywhere_is_none(monkeypatch):
    monkeypatch.delenv("BULK_IMPORT_API_KEY", raising=False)

    assert resolve_api_key(_args()) is None


def test_empty_key_file_is_an_error(tmp_path: Path):
    key_file = tmp_path / "key.txt"
    key_file.write_text("   \n", encoding="utf-8")

    with pytest.raises(ScriptError):
        resolve_api_key(_args(api_key_file=str(key_file)))


def test_client_sends_the_api_key_header():
    client = ApiClient("https://edu.example", api_key="secret")

    assert client._headers()["X-Api-Key"] == "secret"
    assert "Cookie" not in client._headers()


def test_client_sends_both_when_given_both():
    client = ApiClient("https://edu.example", "session=1", api_key="secret")
    headers = client._headers({"Content-Type": "application/json"})

    assert headers["X-Api-Key"] == "secret"
    assert headers["Cookie"] == "session=1"
    assert headers["Content-Type"] == "application/json"


def test_client_sends_no_credentials_header_when_anonymous():
    client = ApiClient("https://edu.example")

    assert "X-Api-Key" not in client._headers()
    assert "Cookie" not in client._headers()
