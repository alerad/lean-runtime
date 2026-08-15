from __future__ import annotations

import json
from pathlib import Path

from lean_runtime.identifier_resolver import IdentifierResolver
from lean_runtime.models import Diagnostic, ExecutionResult
from lean_runtime.projects import ProjectContext


def test_unknown_identifier_uses_the_exact_project_ilean_index(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package = project / ".lake" / "packages" / "mathlib"
    index = package / ".lake" / "build" / "lib" / "lean" / "Mathlib" / "Data.ilean"
    index.parent.mkdir(parents=True)
    index.write_text(
        json.dumps(
            {
                "decls": {"Finset.card_insert_of_notMem": [1, 0, 1, 1]},
                "directImports": [],
                "module": "Mathlib.Data",
                "references": {},
                "version": 3,
            }
        )
    )
    (project / "lakefile.toml").write_text('name = "project"\n')
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.33.0\n")
    (project / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packagesDir": ".lake/packages",
                "packages": [{"name": "mathlib", "type": "git"}],
            }
        )
    )
    context = ProjectContext(
        project,
        "leanprover/lean4:v4.33.0",
        project / "lakefile.toml",
        project / "lake-manifest.json",
    )
    result = ExecutionResult(
        ok=False,
        exit_code=1,
        toolchain=context.toolchain,
        command=("lean", "Main.lean"),
        cwd=str(project),
        stdout="",
        stderr="",
        elapsed_seconds=0.1,
        diagnostics=(Diagnostic("error", "Unknown identifier `card_insert_of_notMemm`"),),
    )

    hints = IdentifierResolver(tmp_path / "runtime").suggestions(context, result)

    assert hints == (
        "Unknown `card_insert_of_notMemm`; did you mean `Finset.card_insert_of_notMem`?",
    )
