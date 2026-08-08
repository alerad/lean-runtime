from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lean_runtime import EnvironmentLock, EnvironmentSpec, GitPackage, Runtime, RuntimeEvent
from lean_runtime.discovery import Catalog, CatalogEntry, Discovery, DiscoveryPolicy
from lean_runtime.run_cli import main as run_main


def _run(repo: Path, *command: str) -> str:
    completed = subprocess.run(
        command,
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-m", message)
    return _run(repo, "git", "rev-parse", "HEAD")


def _fixture_history(root: Path, toolchain: str) -> tuple[Path, str, str, str]:
    repo = root / "fixture"
    repo.mkdir()
    _run(repo, "git", "init", "-q", "-b", "main")
    _run(repo, "git", "config", "user.name", "Lean Discovery Tests")
    _run(repo, "git", "config", "user.email", "discovery@example.invalid")
    _write(repo / "lean-toolchain", toolchain + "\n")
    _write(
        repo / "lakefile.toml",
        'name = "fixture"\nversion = "0.1.0"\ndefaultTargets = ["Fixture"]\n\n'
        '[[lean_lib]]\nname = "Fixture"\n',
    )
    _write(repo / "Fixture.lean", "import Fixture.Legacy\n")
    _write(repo / "Fixture/Legacy.lean", "def Fixture.Legacy.answer : Nat := 42\n")
    old = _commit(repo, "legacy module")

    (repo / "Fixture/Legacy.lean").unlink()
    _write(repo / "Fixture.lean", "import Fixture.Modern\n")
    _write(repo / "Fixture/Modern.lean", "def Fixture.Modern.answer : Nat := 42\n")
    modern_nat = _commit(repo, "modern Nat declaration")

    _write(
        repo / "Fixture/Modern.lean",
        'def Fixture.Modern.answer : String := "42"\n',
    )
    modern_string = _commit(repo, "modern String declaration")
    return repo, old, modern_nat, modern_string


def _entry(
    runtime: Runtime,
    repo: Path,
    revision: str,
    *,
    entry_id: str,
    module: str,
    created_at: str,
    toolchain: str,
) -> CatalogEntry:
    lock = runtime.prepare(
        EnvironmentSpec(
            toolchain=toolchain,
            packages=(
                GitPackage.git(
                    "fixture",
                    str(repo),
                    revision,
                    root_module=module,
                ),
            ),
        )
    )
    return CatalogEntry(
        id=entry_id,
        channel="stable",
        toolchain=toolchain,
        lock=lock,
        modules=frozenset({module}),
        created_at=created_at,
    )


@pytest.mark.integration
def test_newer_candidate_rejects_and_older_candidate_compiles(tmp_path: Path, capsys) -> None:
    toolchain = os.environ.get("LEAN_DISCOVERY_TEST_TOOLCHAIN", "leanprover/lean4:v4.32.2")
    repo, old_revision, nat_revision, string_revision = _fixture_history(tmp_path, toolchain)
    events: list[RuntimeEvent] = []
    runtime = Runtime(
        home=tmp_path / "runtime",
        availability="auto",
        libraries=(),
        on_event=events.append,
    )
    old = _entry(
        runtime,
        repo,
        old_revision,
        entry_id="fixture-legacy",
        module="Fixture.Legacy",
        created_at="2026-01-01T00:00:00Z",
        toolchain=toolchain,
    )
    modern_nat = _entry(
        runtime,
        repo,
        nat_revision,
        entry_id="fixture-modern-nat",
        module="Fixture.Modern",
        created_at="2026-02-01T00:00:00Z",
        toolchain=toolchain,
    )
    modern_string = _entry(
        runtime,
        repo,
        string_revision,
        entry_id="fixture-modern-string",
        module="Fixture.Modern",
        created_at="2026-03-01T00:00:00Z",
        toolchain=toolchain,
    )
    catalog = Catalog(
        generated_at="2026-08-06T00:00:00Z",
        entries=(old, modern_nat, modern_string),
    )
    discovery = Discovery(
        catalog=catalog,
        runtime=runtime,
        runtime_events=events,
        policy=DiscoveryPolicy(
            max_candidates=3,
            max_total_seconds=360,
            candidate_timeout_seconds=180,
            allow_download=False,
            allow_source_build=True,
        ),
    )

    result = discovery.discover_and_check(
        """import Fixture.Modern

example : Fixture.Modern.answer = 42 := rfl
"""
    )

    assert result.status == "found"
    assert result.confidence == "compiled"
    assert result.selected_candidate is not None
    assert result.selected_candidate.entry.id == "fixture-modern-nat"
    assert result.lock_id == modern_nat.lock.lock_id
    assert result.environment_id is not None
    assert [attempt.candidate_id for attempt in result.attempts] == [
        "fixture-modern-string",
        "fixture-modern-nat",
    ]
    assert [attempt.status for attempt in result.attempts] == [
        "lean_rejected",
        "compiled",
    ]
    assert [attempt.acquisition for attempt in result.attempts] == [
        "source_built",
        "source_built",
    ]
    assert all(attempt.execution_result is not None for attempt in result.attempts)
    assert json.loads(json.dumps(result.to_dict()))["status"] == "found"

    catalog_path = tmp_path / "catalog.json"
    catalog.write(catalog_path)
    source_path = tmp_path / "Main.lean"
    source_path.write_text(
        "import Fixture.Modern\n\nexample : Fixture.Modern.answer = 42 := rfl\n",
        encoding="utf-8",
    )
    lock_path = tmp_path / "selected.lock.json"
    assert (
        run_main(
            [
                str(source_path),
                "--catalog",
                str(catalog_path),
                "--home",
                str(runtime.home),
                "--offline",
                "--lock-out",
                str(lock_path),
                "--quiet",
            ]
        )
        == 0
    )
    assert "accepted" in capsys.readouterr().out
    assert EnvironmentLock.load(lock_path).lock_id == modern_nat.lock.lock_id
