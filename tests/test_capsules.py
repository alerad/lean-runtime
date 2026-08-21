from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lean_runtime.capsules import (
    CAPSULE_MANIFEST,
    CapsuleManifest,
    artifact_capability,
    build_manifest,
    inventory_workspace,
    materialize_capsule,
    module_from_artifact,
    parse_import_headers,
    render_setup,
)
from lean_runtime.environments import _capsule_setup, _setup_module_name
from lean_runtime.errors import EnvironmentError
from lean_runtime.lockfiles import EnvironmentLock, LockedPackage


def test_artifact_capabilities_and_module_names() -> None:
    expected = {
        "Mathlib/Tactic.olean": ("check", "Mathlib.Tactic"),
        "Mathlib/Tactic.ir": ("check", "Mathlib.Tactic"),
        "Mathlib/Tactic.ir.sig": ("check", "Mathlib.Tactic"),
        "Mathlib/Tactic.olean.server": ("check", "Mathlib.Tactic"),
        "Mathlib/Tactic.olean.private": ("check", "Mathlib.Tactic"),
        "Mathlib/Tactic.ilean": ("editor", "Mathlib.Tactic"),
        "Mathlib/Tactic.c": ("development", None),
        "Mathlib/Tactic.trace": ("metadata", None),
    }
    for raw, result in expected.items():
        path = Path(raw)
        assert artifact_capability(path) == result[0]
        assert module_from_artifact(path) == result[1]


def test_render_setup_supports_flat_and_grouped_dialects() -> None:
    artifacts = {"Mathlib.Tactic": (("Tactic.olean",), ("Tactic.ir.sig", "Tactic.ir"))}
    flat = render_setup(
        lean_version="v4.32.2", name="Main", package="scratch", import_artifacts=artifacts
    )
    grouped = render_setup(
        lean_version="leanprover/lean4:v4.33.0",
        name="Main",
        package="scratch",
        import_artifacts=artifacts,
    )
    assert flat["importArts"]["Mathlib.Tactic"] == [
        "Tactic.olean",
        "Tactic.ir.sig",
        "Tactic.ir",
    ]
    assert grouped["importArts"]["Mathlib.Tactic"] == [
        ["Tactic.olean"],
        ["Tactic.ir.sig", "Tactic.ir"],
    ]


def test_parse_import_headers_uses_lean_protocol(tmp_path: Path) -> None:
    source = tmp_path / "Main.lean"
    source.write_text("import Alpha.Beta\npublic meta import Gamma\n")
    fake = tmp_path / "fake.py"
    fake.write_text(
        "import json,sys\n"
        "paths=sys.argv[sys.argv.index('--run')+2:]\n"
        "row={'errors': [], 'result': {'imports': "
        "[{'module':'Init'}, {'module':'Alpha.Beta'}, {'module':'Gamma'}]}}\n"
        "print(json.dumps({'imports':[row for _ in paths]}))\n"
    )
    result = parse_import_headers([sys.executable, str(fake)], [source])
    assert result[source] == ("Alpha.Beta", "Gamma", "Init")


def test_inventory_prefers_the_least_stripped_source_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    package = workspace / ".lake" / "packages" / "aesop"
    (package / "AesopTest").mkdir(parents=True)
    (package / "Aesop.lean").write_text("import Exact\n")
    (package / "AesopTest" / "Aesop.lean").write_text("import Wrong\n")
    artifact = package / ".lake" / "build" / "lib" / "lean" / "Aesop.olean"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"olean")
    locked = LockedPackage(
        name="aesop",
        url="https://example.test/aesop",
        revision="a" * 40,
        source_id="source_" + "b" * 64,
        tree_hash="c" * 40,
    )
    lock = EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.2",
        spec_digest="spec_" + "d" * 64,
        root_lakefile='name = "test"\n',
        root_module="import Aesop\n",
        manifest={"version": "1.2.0", "packagesDir": ".lake/packages", "packages": []},
        packages=(locked,),
    )
    fake = tmp_path / "fake.py"
    fake.write_text(
        "import json,sys\n"
        "paths=sys.argv[sys.argv.index('--run')+2:]\n"
        "rows=[]\n"
        "for path in paths:\n"
        "  module='Wrong' if 'AesopTest' in path else 'Exact'\n"
        "  rows.append({'errors': [], 'result': {'imports': [{'module': module}]}})\n"
        "print(json.dumps({'imports': rows}))\n"
    )

    manifest = inventory_workspace(workspace, lock, "env_test", [sys.executable, str(fake)])

    aesop = next(module for module in manifest.modules if module.name == "Aesop")
    assert aesop.imports == ("Exact",)


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    root = workspace / ".lake" / "packages" / "mathlib" / ".lake" / "build" / "lib" / "lean"
    for relative, content in {
        "Mathlib/Tactic.olean": b"public",
        "Mathlib/Tactic.ir": b"ir",
        "Mathlib/Tactic.olean.private": b"proofs",
        "Mathlib/Tactic.olean.server": b"server",
        "Mathlib/Tactic.ilean": b"index",
        "Mathlib/Tactic.c": b"c source",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return workspace, root


def test_manifest_closure_and_physical_capsule(tmp_path: Path) -> None:
    workspace, root = _workspace(tmp_path)
    init = root / "Init.olean"
    init.write_bytes(b"init")
    manifest = build_manifest(
        workspace=workspace,
        environment_id="env_test",
        lock_id="lock_test",
        toolchain="leanprover/lean4:v4.33.0",
        build_roots={"mathlib": root},
        imports={"Mathlib.Tactic": ("Init",), "Init": ()},
    )
    assert [module.name for module in manifest.closure(["Mathlib.Tactic"])] == [
        "Init",
        "Mathlib.Tactic",
    ]
    destination = tmp_path / "capsule"
    copied = materialize_capsule(workspace, destination, manifest)
    assert copied == (len(b"public") + len(b"ir") + len(b"init") + len(b"proofs") + len(b"server"))
    retained = destination / root.relative_to(workspace) / "Mathlib" / "Tactic.olean"
    assert retained.read_bytes() == b"public"
    assert retained.with_suffix(".olean.private").exists()
    assert retained.with_suffix(".olean.server").exists()
    assert not retained.with_suffix(".ilean").exists()
    assert not retained.with_suffix(".c").exists()
    loaded = CapsuleManifest.load(destination / CAPSULE_MANIFEST)
    assert loaded.digest == manifest.digest


def test_materialization_fails_closed_if_an_artifact_changes(tmp_path: Path) -> None:
    workspace, root = _workspace(tmp_path)
    manifest = build_manifest(
        workspace=workspace,
        environment_id="env_test",
        lock_id="lock_test",
        toolchain="v4.32.2",
        build_roots={"mathlib": root},
        imports={},
    )
    (root / "Mathlib" / "Tactic.olean").write_bytes(b"tampered")
    with pytest.raises(EnvironmentError, match="changed during export"):
        materialize_capsule(workspace, tmp_path / "capsule", manifest)
    assert not (tmp_path / "capsule").exists()


def test_manifest_rejects_duplicate_or_unsorted_modules(tmp_path: Path) -> None:
    path = tmp_path / "capsule.json"
    path.write_text(
        json.dumps(
            {
                "schema": "lean-runtime-check-capsule/1",
                "environment_id": "env_test",
                "lock_id": "lock_test",
                "toolchain": "v4.33.0",
                "modules": [
                    {
                        "name": "Z",
                        "package": "sample",
                        "imports": [],
                        "imports_complete": True,
                        "artifacts": [],
                    },
                    {
                        "name": "A",
                        "package": "sample",
                        "imports": [],
                        "imports_complete": True,
                        "artifacts": [],
                    },
                ],
            }
        )
    )
    with pytest.raises(EnvironmentError, match="ordering"):
        CapsuleManifest.load(path)


def test_sparse_execution_setup_uses_versioned_facet_order(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    build = workspace / ".lake" / "packages" / "sample" / ".lake" / "build" / "lib" / "lean"
    for suffix in (".olean", ".ir", ".olean.server", ".olean.private"):
        path = build / f"A{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(suffix.encode())
    lock = EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.2",
        spec_digest="spec_" + "a" * 64,
        root_lakefile='name = "test"\n',
        root_module="import A\n",
        manifest={"version": "1.2.0", "packagesDir": ".lake/packages", "packages": []},
        packages=(),
    )
    manifest = build_manifest(
        workspace=workspace,
        environment_id="env_test",
        lock_id=lock.lock_id,
        toolchain=lock.toolchain,
        build_roots={"sample": build},
        imports={"A": ()},
    )
    capsule = workspace / CAPSULE_MANIFEST
    capsule.parent.mkdir(parents=True)
    capsule.write_text(json.dumps(manifest.to_dict()))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    setup_path = _capsule_setup(workspace, scratch, "import A\n", "Main.lean", lock)
    assert setup_path is not None
    setup = json.loads(setup_path.read_text())
    assert [Path(path).name for path in setup["importArts"]["A"]] == [
        "A.olean",
        "A.ir",
        "A.olean.server",
        "A.olean.private",
    ]


def test_setup_module_names_escape_non_identifier_stems() -> None:
    assert _setup_module_name("Main.lean") == "Main"
    assert _setup_module_name("with space.lean") == "«with space»"
    assert _setup_module_name("dir/héllo 世界.lean") == "dir.«héllo 世界»"
