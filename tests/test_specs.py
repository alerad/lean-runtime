from __future__ import annotations

import json
from pathlib import Path

import pytest

from lean_runtime import EnvironmentSpec, GitPackage, SpecificationError

REV_A = "a" * 40
REV_B = "b" * 40


def test_specification_is_order_canonical() -> None:
    first = GitPackage("alpha", "https://example.test/alpha", REV_A)
    second = GitPackage("beta", "https://example.test/beta", REV_B)
    left = EnvironmentSpec("4.32.0", (first, second))
    right = EnvironmentSpec("leanprover/lean4:v4.32.0", (second, first))
    assert left.to_dict() == right.to_dict()
    assert left.spec_digest == right.spec_digest


def test_package_requires_exact_commit() -> None:
    with pytest.raises(SpecificationError, match="full 40-character"):
        GitPackage("mathlib", "https://example.test/mathlib", "main")


def test_duplicate_direct_names_are_rejected() -> None:
    package = GitPackage("same", "https://example.test/same", REV_A)
    with pytest.raises(SpecificationError, match="duplicate"):
        EnvironmentSpec("4.32.0", (package, package))


def test_load_toml_and_json(tmp_path: Path) -> None:
    document = {
        "toolchain": "4.32.0",
        "packages": [
            {
                "name": "sample",
                "url": "https://example.test/sample",
                "rev": REV_A,
                "root_module": "Sample",
            }
        ],
    }
    json_path = tmp_path / "environment.json"
    json_path.write_text(json.dumps(document))
    toml_path = tmp_path / "environment.toml"
    toml_path.write_text(
        f'toolchain = "4.32.0"\n\n[[packages]]\nname = "sample"\n'
        f'url = "https://example.test/sample"\nrev = "{REV_A}"\n'
        'root_module = "Sample"\n'
    )
    assert EnvironmentSpec.load(json_path).to_dict() == EnvironmentSpec.load(toml_path).to_dict()
