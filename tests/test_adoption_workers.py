"""Bounded read-only planning, deduplication, and fresh identity snapshots."""

import json
import threading
from collections import Counter

import pytest

from lean_runtime import _adoption_workers as sizing
from lean_runtime import project_sharing as sharing
from lean_runtime.cli import main
from lean_runtime.errors import ProjectError
from lean_runtime.events import EventEmitter, activated, current


def projects(tmp_path):
    deps = [tmp_path / "sources" / name for name in ("a", "b")]
    for dep in deps:
        dep.mkdir(parents=True)
        (dep / "Main.lean").write_text("example : True := trivial\n")
    root = tmp_path / "projects"
    for name in ("one", "two", "three"):
        project = root / name
        project.mkdir(parents=True)
        (project / "lean-toolchain").write_text("leanprover/lean4:v4.30.0-rc2\n")
        (project / "lakefile.toml").write_text(f'name = "{name}"\n')
        (project / "lake-manifest.json").write_text(
            json.dumps(
                {
                    "version": "1.2.0",
                    "name": name,
                    "packagesDir": ".lake/packages",
                    "packages": [
                        {
                            "type": "path",
                            "name": dep.name,
                            "dir": str(dep),
                            "manifestFile": "lake-manifest.json",
                            "configFile": "lakefile.toml",
                        }
                        for dep in deps
                    ],
                }
            )
        )
    return root, deps


def test_parallel_hashes_once_and_fresh_next_plan(tmp_path, monkeypatch):
    root, deps = projects(tmp_path)
    original = sharing.source_snapshot_digest
    calls = Counter()
    hashes = {}
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    events = []

    def digest(path):
        # Fails if the two distinct source trees are not hashed concurrently.
        barrier.wait(timeout=5)
        value = original(path)
        with lock:
            calls[path] += 1
            hashes[path] = value
        current().emit("test.worker", "hash completed")
        return value

    monkeypatch.setattr(sharing, "source_snapshot_digest", digest)
    with activated(EventEmitter(events.append)):
        parallel = sharing.plan_adoption(root, recursive=True, jobs=2)
    assert calls == {dep: 1 for dep in deps}
    assert sum(e.kind == "test.worker" for e in events) == 2
    first = hashes.copy()
    (deps[0] / "Main.lean").write_text("example : 1 = 1 := rfl\n")
    sharing.plan_adoption(root, recursive=True, jobs=2)
    assert calls == {dep: 2 for dep in deps}
    assert hashes[deps[0]] != first[deps[0]]
    monkeypatch.setattr(sharing, "source_snapshot_digest", original)
    serial = sharing.plan_adoption(root, recursive=True, jobs=1)
    a, b = parallel.to_dict(), serial.to_dict()
    a.pop("jobs")
    b.pop("jobs")
    assert a == b
    assert [p.root.name for p in parallel.projects] == ["one", "three", "two"]


def test_parallel_inspection_is_bounded(tmp_path, monkeypatch):
    root, _ = projects(tmp_path)
    original = sharing.inspect_adoption
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = peak = started = 0

    def inspect(path):
        nonlocal active, peak, started
        with lock:
            active += 1
            started += 1
            number = started
            peak = max(peak, active)
        if number <= 2:
            barrier.wait(timeout=5)
        try:
            return original(path)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(sharing, "inspect_adoption", inspect)
    assert sharing.plan_adoption(root, recursive=True, jobs=2).ready == 3
    assert peak == 2


@pytest.mark.parametrize(
    ("cpus", "memory", "expected"),
    [
        (64, 64 * 1024**3, 8),
        (4, 8 * 1024**3, 3),
        (32, 1536 * 1024**2, 2),
        (32, 128 * 1024**2, 1),
        (32, None, 1),
    ],
)
def test_auto_sizing(monkeypatch, cpus, memory, expected):
    monkeypatch.setattr(sizing, "available_cpus", lambda: cpus)
    monkeypatch.setattr(sizing, "available_memory_bytes", lambda: memory)
    assert sizing.adoption_jobs() == expected
    assert sizing.adoption_jobs(11) == 11


@pytest.mark.parametrize("jobs", [0, -1])
def test_invalid_jobs(jobs):
    with pytest.raises(ProjectError, match="positive integer"):
        sizing.adoption_jobs(jobs)


def test_cli_jobs(tmp_path, capsys):
    root, _ = projects(tmp_path)
    assert main(["adopt", str(root), "--dry-run", "--jobs", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"] == 1
    assert main(["adopt", str(root), "--dry-run", "--jobs", "0", "--json"]) == 2
    assert "positive integer" in capsys.readouterr().out
