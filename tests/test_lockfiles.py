from __future__ import annotations

import json
from pathlib import Path

import pytest

from lean_runtime import EnvironmentError, EnvironmentLock, LockedPackage


def sample_lock() -> EnvironmentLock:
    return EnvironmentLock(
        toolchain="leanprover/lean4:v4.32.0",
        spec_digest="spec_abc",
        root_lakefile='name = "test"\n',
        root_module="/- root -/\n",
        manifest={"version": "1.1.0", "packages": []},
        packages=(
            LockedPackage(
                name="sample",
                url="https://example.test/sample",
                revision="a" * 40,
                source_id="source_abc",
                tree_hash="b" * 40,
            ),
        ),
    )


def test_lock_round_trip(tmp_path: Path) -> None:
    lock = sample_lock()
    path = tmp_path / "environment.lock.json"
    lock.write(path)
    restored = EnvironmentLock.load(path)
    assert restored == lock
    assert restored.lock_id == lock.lock_id


def test_lock_tampering_is_detected(tmp_path: Path) -> None:
    lock = sample_lock()
    payload = lock.to_dict()
    payload["toolchain"] = "leanprover/lean4:v4.31.0"
    path = tmp_path / "environment.lock.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(EnvironmentError, match="identity mismatch"):
        EnvironmentLock.load(path)
