from __future__ import annotations

import os
import sys
from pathlib import Path

from lean_runtime.events import EventEmitter, RuntimeEvent
from lean_runtime.lake_cache import LakeArtifactCache
from lean_runtime.projects import discover_project


class ProbeToolchains:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.calls: list[tuple[str, ...]] = []

    @property
    def environment(self) -> dict[str, str]:
        return os.environ.copy()

    def command(self, _toolchain: str, executable: str, *args: str) -> list[str]:
        self.calls.append((executable, *args))
        output = ""
        if args == ("build", "--help"):
            output = "lake build ... -o mappings\n"
        elif args == ("cache", "add", "--help"):
            output = "lake cache add mappings --service URL\n"
        return [sys.executable, "-c", f"print({output!r})"]


def _context(root: Path, *, opted_in: bool = True):
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.33.0\n")
    setting = "enableArtifactCache = true\n" if opted_in else ""
    (root / "lakefile.toml").write_text(setting + 'name = "fixture"\n')
    return discover_project(root)


def test_capability_probe_is_persistent_and_warm_load_is_silent(tmp_path: Path) -> None:
    events: list[RuntimeEvent] = []
    toolchains = ProbeToolchains(tmp_path)
    cache = LakeArtifactCache(tmp_path, toolchains, EventEmitter(events.append))  # type: ignore[arg-type]

    observed = cache.capabilities("4.33.0")

    assert observed.supported
    assert len(toolchains.calls) == 2
    assert [event.kind for event in events] == [
        "lake_cache.capability_probe_started",
        "lake_cache.capability_probe_finished",
    ]

    warm_events: list[RuntimeEvent] = []
    warm = LakeArtifactCache(tmp_path, toolchains, EventEmitter(warm_events.append))  # type: ignore[arg-type]
    assert warm.capabilities("4.33.0") == observed
    assert len(toolchains.calls) == 2
    assert warm_events == []


def test_root_only_environment_is_opt_in_and_abi_keyed(tmp_path: Path, monkeypatch) -> None:
    toolchains = ProbeToolchains(tmp_path / "runtime")
    cache = LakeArtifactCache(toolchains.home, toolchains, EventEmitter())  # type: ignore[arg-type]
    context = _context(tmp_path / "project")

    environment = cache.environment(context)

    assert environment["LAKE_ARTIFACT_CACHE"] == "false"
    assert environment["LAKE_RESTORE_ARTIFACTS"] == "true"
    assert environment["LAKE_CACHE_DIR"] == str(cache.cache_root(context.toolchain))
    decision = cache.decision(context)
    assert decision.outcome == "accepted"
    assert decision.details is not None
    assert decision.details["scope"] == "root-package-only"
    assert decision.details["remote_mappings"] == "disabled_without_sha256_inventory"

    other = _context(tmp_path / "ordinary", opted_in=False)
    ordinary = cache.environment(other)
    assert "LAKE_CACHE_DIR" not in ordinary
    assert cache.decision(other).reason == "project_not_opted_in"

    original_key = cache.key(context.toolchain)
    monkeypatch.setattr(
        "lean_runtime.lake_cache.platform_compatibility",
        lambda: {"schema": "test", "system": "linux", "machine": "x86_64", "abi": "gnu"},
    )
    assert cache.key(context.toolchain) != original_key
