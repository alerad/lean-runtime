"""Slim check-profile toolchains: exclusion rules, materialization, routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from lean_runtime.errors import ToolchainError
from lean_runtime.toolchain_slim import SlimManifest, is_excluded, materialize
from lean_runtime.toolchains import ToolchainManager

TOOLCHAIN = "leanprover/lean4:v9.9.9"
DIR_NAME = "leanprover--lean4---v9.9.9"


def test_exclusion_rules_match_the_check_profile() -> None:
    kept = [
        "bin/lean",
        "bin/lake",
        "lib/lean/Init.olean",
        "lib/lean/Init.olean.private",
        "lib/lean/Init.olean.server",
        "lib/lean/Init/Prelude.ir",
        "lib/lean/libleanshared.dylib",
        "LICENSE",
    ]
    dropped = [
        "lib/lean/Init.ilean",
        "lib/lean/libLean.a",
        "lib/libLLVM.dylib",
        "lib/libclang-cpp.dylib",
        "lib/clang/20/include/stdint.h",
        "lib/libc/musl.o",
        "src/lean/kernel.cpp",
    ]
    for path in kept:
        assert not is_excluded(Path(path)), path
    for path in dropped:
        assert is_excluded(Path(path)), path


def _fake_toolchain(root: Path) -> Path:
    source = root / "full"
    for relative in (
        "bin/lean",
        "bin/lake",
        "lib/lean/Init.olean",
        "lib/lean/Init.olean.private",
        "lib/lean/Init/Prelude.ir",
        "lib/lean/Init.ilean",
        "lib/lean/libLean.a",
        "lib/libLLVM.dylib",
        "src/lean/kernel.cpp",
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    return source


def test_materialize_drops_excluded_classes_and_hardlinks(tmp_path: Path) -> None:
    source = _fake_toolchain(tmp_path)
    destination = tmp_path / "slim"
    manifest = materialize(
        source, destination, toolchain=TOOLCHAIN, created_at="2026-08-11T00:00:00+00:00"
    )
    assert (destination / "bin" / "lean").is_file()
    assert (destination / "lib" / "lean" / "Init.olean.private").is_file()
    assert not (destination / "lib" / "lean" / "Init.ilean").exists()
    assert not (destination / "lib" / "libLLVM.dylib").exists()
    assert not (destination / "src").exists()
    assert manifest.files == 5
    assert manifest.excluded_files == 4
    # Hardlinked materialization: the same inode backs source and slim copies.
    assert (destination / "bin" / "lean").stat().st_ino == (source / "bin" / "lean").stat().st_ino
    loaded = SlimManifest.load(destination)
    assert loaded is not None
    assert loaded.toolchain == TOOLCHAIN


def test_materialize_rejects_a_non_toolchain_source(tmp_path: Path) -> None:
    with pytest.raises(ToolchainError, match="not an installed Lean toolchain"):
        materialize(
            tmp_path / "missing",
            tmp_path / "slim",
            toolchain=TOOLCHAIN,
            created_at="2026-08-11T00:00:00+00:00",
        )


def _manager_with_slim(tmp_path: Path) -> ToolchainManager:
    manager = ToolchainManager(tmp_path)
    source = _fake_toolchain(tmp_path)
    materialize(
        source,
        manager.slim_path(TOOLCHAIN),
        toolchain=TOOLCHAIN,
        created_at="2026-08-11T00:00:00+00:00",
    )
    return manager


def test_command_uses_the_slim_copy_when_the_full_toolchain_is_gone(tmp_path: Path) -> None:
    manager = _manager_with_slim(tmp_path)
    command = manager.command(TOOLCHAIN, "lean", "Main.lean")
    assert command == [str(manager.slim_path(TOOLCHAIN) / "bin" / "lean"), "Main.lean"]


def test_command_rejects_executables_outside_the_slim_profile(tmp_path: Path) -> None:
    manager = _manager_with_slim(tmp_path)
    with pytest.raises(ToolchainError, match="does not provide 'leanc'"):
        manager.command(TOOLCHAIN, "leanc", "file.c")


def test_ensure_short_circuits_on_a_slim_copy(tmp_path: Path) -> None:
    manager = _manager_with_slim(tmp_path)
    # No Elan bootstrap, no network: the slim copy satisfies the toolchain.
    assert manager.ensure(TOOLCHAIN) == TOOLCHAIN


def test_prune_refuses_without_a_verified_slim_copy(tmp_path: Path) -> None:
    manager = ToolchainManager(tmp_path)
    with pytest.raises(ToolchainError, match="refusing to prune"):
        manager.prune_original(TOOLCHAIN)
