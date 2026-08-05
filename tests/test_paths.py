from pathlib import Path

from lean_runtime._paths import remove_tree


def test_remove_tree_retries_transient_permission_errors(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "tree"
    target.mkdir()
    calls = 0

    def flaky_rmtree(path: Path, *, onerror) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("Git pack is briefly locked")
        assert onerror is not None
        path.rmdir()

    monkeypatch.setattr("lean_runtime._paths.shutil.rmtree", flaky_rmtree)
    monkeypatch.setattr("lean_runtime._paths.time.sleep", lambda _seconds: None)

    remove_tree(target)

    assert calls == 2
    assert not target.exists()
