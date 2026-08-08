from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lean_runtime import EnvironmentLock, LockedPackage, Runtime
from lean_runtime.discovery import Catalog, CatalogError, CatalogSourceManifest, build_catalog_file
from lean_runtime.discovery.module_inventory import modules_from_source


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _fixture_source(root: Path) -> tuple[Path, str, str]:
    repo = root / "fixture"
    (repo / "Fixture/Sub").mkdir(parents=True)
    (repo / "Fixture.lean").write_text("import Fixture.Basic\n", encoding="utf-8")
    (repo / "Fixture/Basic.lean").write_text("def Fixture.answer := 42\n", encoding="utf-8")
    (repo / "Fixture/Sub/Extra.lean").write_text("def Fixture.extra := 7\n", encoding="utf-8")
    (repo / "NotFixture.lean").write_text("def ignored := true\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Lean Discovery Tests")
    _git(repo, "config", "user.email", "discovery@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")


def _lock(root: Path) -> tuple[Runtime, EnvironmentLock]:
    repo, revision, tree_hash = _fixture_source(root)
    runtime = Runtime(home=root / "runtime", libraries=())
    package = LockedPackage(
        name="fixture",
        url=repo.resolve().as_uri(),
        revision=revision,
        tree_hash=tree_hash,
        source_id=f"source_{'a' * 64}",
        root_module="Fixture",
    )
    runtime.store.publish_source(
        repo,
        package.source_id,
        {
            "source_id": package.source_id,
            "url": package.url,
            "revision": revision,
            "tree_hash": tree_hash,
        },
    )
    lock = EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.2",
        spec_digest=f"spec_{'b' * 64}",
        root_lakefile='name = "fixture"\n',
        root_module="import Fixture\n",
        manifest={"version": "1.1.0", "packagesDir": ".lake/packages", "packages": []},
        packages=(package,),
    )
    return runtime, lock


def test_modules_are_limited_to_the_locked_root(tmp_path: Path) -> None:
    runtime, lock = _lock(tmp_path)
    package = lock.packages[0]
    source = runtime.store.validate_source(
        package.source_id,
        url=package.url,
        revision=package.revision,
        tree_hash=package.tree_hash,
    )
    assert modules_from_source(source, package) == {
        "Fixture",
        "Fixture.Basic",
        "Fixture.Sub.Extra",
    }


def test_builder_is_deterministic_and_reloads_public_catalog(tmp_path: Path) -> None:
    runtime, lock = _lock(tmp_path)
    lock_path = tmp_path / "locks/fixture.lock.json"
    lock_path.parent.mkdir()
    lock.write(lock_path)
    manifest = tmp_path / "environments.toml"
    manifest.write_text(
        """schema = "lean-runtime.discovery.catalog-source/v1"
generated_at = "2026-08-08T00:00:00Z"

[[environment]]
id = "fixture"
channel = "stable"
lock = "locks/fixture.lock.json"
inventory_packages = ["fixture"]
library_hints = ["oci+http://127.0.0.1:5000/owner/discovery"]
created_at = "2026-08-01T00:00:00Z"
""",
        encoding="utf-8",
    )
    output = tmp_path / "catalog.json"
    first = build_catalog_file(manifest, output, runtime=runtime)
    first_bytes = output.read_bytes()
    second = build_catalog_file(manifest, output, runtime=runtime)
    assert output.read_bytes() == first_bytes
    assert second.digest == first.digest
    assert Catalog.from_file(output).entries[0].modules == {
        "Fixture",
        "Fixture.Basic",
        "Fixture.Sub.Extra",
    }


def test_manifest_rejects_unknown_fields(tmp_path: Path) -> None:
    manifest = tmp_path / "environments.toml"
    manifest.write_text(
        """schema = "lean-runtime.discovery.catalog-source/v1"
generated_at = "2026-08-08T00:00:00Z"
surprise = true
environment = []
""",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="unknown"):
        CatalogSourceManifest.from_file(manifest)


def test_builder_requires_selected_package(tmp_path: Path) -> None:
    runtime, lock = _lock(tmp_path)
    lock_path = tmp_path / "fixture.lock.json"
    lock.write(lock_path)
    manifest = tmp_path / "environments.toml"
    manifest.write_text(
        """schema = "lean-runtime.discovery.catalog-source/v1"
generated_at = "2026-08-08T00:00:00Z"

[[environment]]
id = "fixture"
channel = "stable"
lock = "fixture.lock.json"
inventory_packages = ["missing"]
created_at = "2026-08-01T00:00:00Z"
""",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="lacks inventory packages"):
        build_catalog_file(manifest, tmp_path / "catalog.json", runtime=runtime)


def test_builder_accepts_explicit_core_modules_without_package_sources(tmp_path: Path) -> None:
    runtime, lock = _lock(tmp_path)
    lock_path = tmp_path / "core.lock.json"
    lock.write(lock_path)
    manifest = tmp_path / "environments.toml"
    manifest.write_text(
        """schema = "lean-runtime.discovery.catalog-source/v1"
generated_at = "2026-08-08T00:00:00Z"

[[environment]]
id = "core"
channel = "stable"
lock = "core.lock.json"
inventory_packages = []
modules = ["Init", "Lean", "Std"]
created_at = "2026-08-01T00:00:00Z"
""",
        encoding="utf-8",
    )
    catalog = build_catalog_file(manifest, tmp_path / "catalog.json", runtime=runtime)
    assert catalog.entries[0].modules == {"Init", "Lean", "Std"}
