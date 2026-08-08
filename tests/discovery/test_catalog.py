import json

import pytest
from conftest import make_entry

from lean_runtime.discovery import Catalog, CatalogError


def test_catalog_roundtrip_and_digest_are_canonical() -> None:
    first = make_entry("a", "a", modules=("Z", "A"))
    second = make_entry("b", "b")
    catalog = Catalog(generated_at="2026-08-06T00:00:00Z", entries=(second, first))
    loaded = Catalog.from_bytes(catalog.canonical_bytes())
    assert loaded.digest == catalog.digest
    assert [entry["id"] for entry in loaded.to_dict()["entries"]] == ["a", "b"]


def test_unknown_catalog_field_is_rejected() -> None:
    with pytest.raises(CatalogError, match="unknown"):
        Catalog.from_dict(
            {
                "schema": "lean-runtime.discovery.catalog/v1",
                "generated_at": "2026-08-06T00:00:00Z",
                "entries": [],
                "surprise": True,
            }
        )


def test_duplicate_lock_is_rejected() -> None:
    first = make_entry("a", "a")
    second_value = first.to_dict()
    second_value["id"] = "b"
    second = type(first).from_dict(second_value)
    with pytest.raises(CatalogError, match="duplicate exact locks"):
        Catalog(generated_at="2026-08-06T00:00:00Z", entries=(first, second))


def test_tampered_lock_identity_is_rejected() -> None:
    value = make_entry("a", "a").to_dict()
    value["lock"]["root_module"] = "tampered"
    payload = {
        "schema": "lean-runtime.discovery.catalog/v1",
        "generated_at": "2026-08-06T00:00:00Z",
        "entries": [value],
    }
    with pytest.raises(CatalogError, match="invalid Runtime lock"):
        Catalog.from_bytes(json.dumps(payload).encode())


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(CatalogError, match="timezone"):
        Catalog(generated_at="2026-08-06T00:00:00", entries=())


def test_invalid_module_name_is_rejected() -> None:
    with pytest.raises(CatalogError, match="invalid module"):
        make_entry("bad", "c", modules=("Not a module",))
