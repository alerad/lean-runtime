from __future__ import annotations

import hashlib
import io
import os
import subprocess
from pathlib import Path

import pytest

from lean_runtime import ProjectError, ToolchainError, normalize_toolchain, project_toolchain
from lean_runtime.toolchains import ToolchainManager


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4.32.0", "leanprover/lean4:v4.32.0"),
        ("v4.31.0", "leanprover/lean4:v4.31.0"),
        ("leanprover/lean4:v4.30.0", "leanprover/lean4:v4.30.0"),
        ("nightly-2026-01-01", "nightly-2026-01-01"),
    ],
)
def test_normalize_toolchain(raw: str, expected: str) -> None:
    assert normalize_toolchain(raw) == expected


def test_empty_toolchain_is_rejected() -> None:
    with pytest.raises(ToolchainError):
        normalize_toolchain("  ")


def test_project_toolchain(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    assert project_toolchain(tmp_path) == "leanprover/lean4:v4.32.0"


def test_project_without_toolchain_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProjectError):
        project_toolchain(tmp_path)


def test_explicit_elan_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / ("elan.exe" if os.name == "nt" else "elan")
    executable.write_text("")
    monkeypatch.setenv("LEAN_RUNTIME_ELAN", str(executable))
    assert ToolchainManager(tmp_path / "runtime").elan_path() == executable.absolute()


@pytest.mark.skipif(os.name == "nt", reason="Windows runners may not permit symlink creation")
def test_elan_override_preserves_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elan-init"
    target.write_text("")
    link = tmp_path / "elan"
    link.symlink_to(target)
    monkeypatch.setenv("LEAN_RUNTIME_ELAN", str(link))
    assert ToolchainManager(tmp_path / "runtime").elan_path() == link.absolute()


def test_elan_bootstrap_rejects_installer_with_wrong_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: io.BytesIO(b"bad"))
    with pytest.raises(ToolchainError, match="integrity check"):
        ToolchainManager(tmp_path / "runtime").bootstrap_elan()


def test_is_installed_lists_toolchains_without_running_lean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "elan"
    executable.write_text("")
    monkeypatch.setenv("LEAN_RUNTIME_ELAN", str(executable))
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "leanprover/lean4:v4.32.0 (default)\nleanprover/lean4:v4.31.0\n",
        )

    monkeypatch.setattr(subprocess, "run", run)
    manager = ToolchainManager(tmp_path / "runtime")
    assert manager.is_installed("4.32.0")
    assert not manager.is_installed("4.30.0")
    assert all(command[1:] == ["toolchain", "list"] for command in calls)


def test_executable_digest_identifies_the_exact_toolchain_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ToolchainManager(tmp_path / "runtime")
    toolchain = "leanprover/lean4:v4.33.0"
    binary = manager._elan_toolchain_dir(toolchain) / "bin" / "lake"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"exact lake binary")
    monkeypatch.setattr(manager, "ensure", lambda _toolchain: toolchain)

    assert manager.executable_digest(toolchain, "lake") == (
        "sha256:" + hashlib.sha256(b"exact lake binary").hexdigest()
    )
