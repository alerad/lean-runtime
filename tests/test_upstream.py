from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime import upstream
from lean_runtime.cli import main

LS_REMOTE = "\n".join(
    f"{'0' * 40}\trefs/tags/{tag}"
    for tag in ("v4.9.0", "v4.34.1", "v4.35.0-rc3", "v4.33.1", "nightly-testing-2026-09-30")
)


def test_newest_stable_tag_orders_numerically_and_ignores_prereleases() -> None:
    assert upstream.newest_stable_tag(LS_REMOTE) == "4.34.1"
    assert upstream.newest_stable_tag("") is None


def _fake_git(output: str, exit_code: int = 0):
    def run_git(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(ok=exit_code == 0, stdout=output)

    return run_git


def test_newer_upstream_reports_only_strictly_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(upstream.DISABLE_ENVIRONMENT)
    monkeypatch.setattr(upstream, "run_git", _fake_git(LS_REMOTE))
    assert upstream.newer_upstream_mathlib("4.33.1") == "4.34.1"
    assert upstream.newer_upstream_mathlib("4.34.1") is None


def test_newer_upstream_treats_failures_and_opt_out_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upstream, "run_git", _fake_git(LS_REMOTE, exit_code=128))
    assert upstream.newer_upstream_mathlib("4.33.1") is None  # opted out by conftest
    monkeypatch.delenv(upstream.DISABLE_ENVIRONMENT)
    assert upstream.newer_upstream_mathlib("4.33.1") is None


def _plan(target: Path) -> SimpleNamespace:
    return SimpleNamespace(
        action="create",
        root=target,
        project_name="Proof",
        mathlib_version="4.33.1",
        toolchain="leanprover/lean4:v4.33.1",
        toolchain_installed=True,
        seed_root=None,
        download_bytes=0,
        download_bytes_complete=True,
        blockers=(),
        ready=True,
    )


def _stub_creation(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    result = SimpleNamespace(root=target, packages=9, to_dict=lambda: {"root": str(target)})
    monkeypatch.setattr(
        "lean_runtime.cli.Runtime.plan_project_init", lambda *_a, **_k: _plan(target)
    )
    monkeypatch.setattr("lean_runtime.cli.Runtime.init_project", lambda *_a, **_k: result)


def test_new_names_a_newer_upstream_release_on_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "Proof"
    _stub_creation(monkeypatch, target)
    monkeypatch.setattr("lean_runtime.cli.newer_upstream_mathlib", lambda _v, **_k: "4.34.1")
    home = str(tmp_path / "home")
    assert main(["--home", home, "new", str(target), "--yes", "--json"]) == 0
    captured = capsys.readouterr()
    assert "Mathlib 4.34.1 is released upstream" in captured.err
    assert "4.34.1" not in captured.out


def test_new_skips_the_upstream_lookup_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "Proof"
    _stub_creation(monkeypatch, target)

    def forbidden(_version: str, **_kwargs: object) -> str:
        raise AssertionError("offline creation must not contact upstream")

    monkeypatch.setattr("lean_runtime.cli.newer_upstream_mathlib", forbidden)
    home = str(tmp_path / "home")
    assert main(["--home", home, "new", str(target), "--yes", "--offline"]) == 0
    assert "upstream" not in capsys.readouterr().err
