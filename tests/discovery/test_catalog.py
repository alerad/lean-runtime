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


def test_legacy_library_hints_are_accepted_but_not_reemitted() -> None:
    value = make_entry("a", "a").to_dict()
    value["library_hints"] = ["legacy"]
    loaded = type(make_entry("b", "b")).from_dict(value)
    assert "library_hints" not in loaded.to_dict()


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


def test_catalog_builds_module_package_and_lock_indexes(sample_catalog) -> None:  # type: ignore[no-untyped-def]
    old = next(entry for entry in sample_catalog.entries if entry.id == "mathlib-old")
    assert sample_catalog.entry_ids_for_modules(("Mathlib.Legacy",)) == frozenset({"mathlib-old"})
    assert sample_catalog.entry_ids_for_packages(frozenset({"mathlib"})) == frozenset(
        {"mathlib-old", "mathlib-new"}
    )
    assert sample_catalog.entry_for_lock(old.lock.lock_id) is old
