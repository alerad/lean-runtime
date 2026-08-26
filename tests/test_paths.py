import os
from pathlib import Path

import pytest

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


def test_is_link_recognizes_symlinks_and_plain_directories(tmp_path: Path) -> None:
    from lean_runtime._paths import is_link

    target = tmp_path / "target"
    target.mkdir()
    assert not is_link(target)
    assert not is_link(tmp_path / "missing")
    link = tmp_path / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not permitted here")
    assert is_link(link)


@pytest.mark.skipif(os.name != "nt", reason="directory junctions exist only on Windows")
def test_is_link_recognizes_windows_junctions(tmp_path: Path) -> None:
    import _winapi

    from lean_runtime._paths import is_link

    target = tmp_path / "target"
    target.mkdir()
    (target / "inside.txt").write_text("x")
    junction = tmp_path / "junction"
    _winapi.CreateJunction(str(target), str(junction))
    assert is_link(junction)
    assert not junction.is_symlink()
    assert (junction / "inside.txt").read_text() == "x"
    assert junction.resolve() == target.resolve()


def test_link_directory_falls_back_to_a_junction_when_symlinks_need_privileges(
    tmp_path: Path, monkeypatch
) -> None:
    from lean_runtime import _paths
    from lean_runtime._paths import is_link, link_directory

    target = tmp_path / "target"
    target.mkdir()
    (target / "inside.txt").write_text("x")
    link = tmp_path / "link"

    def denied(*_args, **_kwargs) -> None:
        error = OSError(22, "A required privilege is not held by the client")
        error.winerror = 1314  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(_paths.os, "symlink", denied)
    if os.name != "nt":
        with pytest.raises(OSError):
            link_directory(target, link)
        return
    link_directory(target, link)
    assert is_link(link)
    assert not link.is_symlink()
    assert (link / "inside.txt").read_text() == "x"
    # Removing the link must not touch the target's contents.
    link.unlink()
    assert not link.exists()
    assert (target / "inside.txt").is_file()


def test_link_directory_reraises_other_symlink_failures(tmp_path: Path, monkeypatch) -> None:
    from lean_runtime import _paths
    from lean_runtime._paths import link_directory

    def broken(*_args, **_kwargs) -> None:
        raise OSError(13, "permission denied")

    monkeypatch.setattr(_paths.os, "symlink", broken)
    with pytest.raises(OSError):
        link_directory(tmp_path, tmp_path / "link")
