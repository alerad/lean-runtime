from __future__ import annotations

import json

import pytest

import lean_runtime as lean


def test_schema_resource_names_are_closed() -> None:
    assert {
        "check-batch-v1.schema.json",
        "comparison-v1.schema.json",
        "execution-v1.schema.json",
        "cleanup-v1.schema.json",
        "inspect-v1.schema.json",
        "matrix-v1.schema.json",
        "plan-v1.schema.json",
        "profile-v1.schema.json",
        "publication-v1.schema.json",
        "verify-v1.schema.json",
    } == lean.SCHEMA_NAMES
    with pytest.raises(ValueError, match="unknown Lean Runtime schema"):
        lean.schema_path("../execution-v1.schema.json")


def test_schema_resource_is_available_from_source_checkout() -> None:
    schema = json.loads(lean.schema_path("execution-v1.schema.json").read_text())
    assert schema["$id"].endswith("/execution-v1.schema.json")
