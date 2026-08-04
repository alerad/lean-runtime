from pathlib import Path

from lean_runtime.audit import artifact_inventory


def test_artifact_inventory_is_relocatable_and_ignores_non_build_files(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for workspace in (first, second):
        build = workspace / ".lake" / "build" / "lib"
        build.mkdir(parents=True)
        (build / "Module.olean").write_bytes(b"compiled")
        (workspace / "Main.lean").write_text("def main := 1\n")
    assert artifact_inventory(first) == artifact_inventory(second)
    (second / "Main.lean").write_text("def main := 2\n")
    assert artifact_inventory(first) == artifact_inventory(second)
    (second / ".lake" / "build" / "lib" / "Module.olean").write_bytes(b"changed")
    assert artifact_inventory(first).digest != artifact_inventory(second).digest
