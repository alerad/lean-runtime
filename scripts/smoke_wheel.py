"""Exercise the installed wheel without importing from the source checkout."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import venv
from pathlib import Path


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--lean", action="store_true", help="run a real standalone Lean check")
    arguments = parser.parse_args()
    wheel = arguments.wheel.resolve()
    if not wheel.is_file():
        parser.error(f"wheel does not exist: {wheel}")

    with tempfile.TemporaryDirectory(prefix="lean-runtime-wheel-smoke-") as raw:
        root = Path(raw)
        environment_root = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment_root)
        scripts = environment_root / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        clean_environment = {
            "PATH": str(scripts) + os.pathsep + os.defpath,
            "LEAN_RUNTIME_HOME": str(root / "runtime"),
            "PYTHONNOUSERSITE": "1",
        }
        _run(
            [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
            cwd=root,
            environment=clean_environment,
        )
        probe = (
            "import json, lean_runtime as lean; "
            "path=lean.schema_path('execution-v1.schema.json'); "
            "value=json.loads(path.read_text()); "
            "assert value['$id'].endswith('/execution-v1.schema.json'); "
            "assert lean.__version__ == '1.0.0'"
        )
        _run([str(python), "-c", probe], cwd=root, environment=clean_environment)
        _run([str(scripts / "lean-runtime"), "--help"], cwd=root, environment=clean_environment)
        _run([str(scripts / "lean-run"), "--help"], cwd=root, environment=clean_environment)
        if arguments.lean:
            source = root / "Main.lean"
            source.write_text(
                "-- /// lean-runtime\n"
                '-- toolchain = "leanprover/lean4:v4.32.0"\n'
                "-- ///\n\n"
                "example : True := by trivial\n",
                encoding="utf-8",
            )
            _run([str(scripts / "lean-run"), str(source)], cwd=root, environment=clean_environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
