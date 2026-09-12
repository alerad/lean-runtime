"""Cleanup failures after commit must not roll back partially removed backups."""

import pytest
from test_projects import ProjectToolchains, _shared_project

from lean_runtime import Runtime, discover_project


@pytest.mark.parametrize("detach", [False, True])
def test_backup_cleanup_failure_does_not_rollback_committed_transaction(
    tmp_path, monkeypatch, detach
):
    from lean_runtime import project_sharing as sharing

    source, _ = _shared_project(tmp_path / "first", tmp_path / "dependency")
    runtime = Runtime(toolchains=ProjectToolchains(tmp_path / "runtime"), libraries=[])
    context = discover_project(source)
    if detach:
        assert runtime.attach_projects(context.root).ok
    remove = sharing._remove_path

    def fail_backup(path):
        if "backup" in path.name:
            # Simulate a partially deleted backup; restoration would be unsafe.
            remove(path)
            raise OSError("cleanup failed after deletion")
        remove(path)

    monkeypatch.setattr(sharing, "_remove_path", fail_backup)
    if detach:
        runtime.project_adopter.detach(context, probe=lambda _: None)
        assert not (context.root / ".lake/packages/dep").is_symlink()
        assert not (context.root / ".lake/lean-runtime-attachment.json").exists()
    else:
        assert runtime.attach_projects(context.root).ok
        assert (context.root / ".lake/packages/dep").is_symlink()
        assert (context.root / ".lake/lean-runtime-attachment.json").exists()
