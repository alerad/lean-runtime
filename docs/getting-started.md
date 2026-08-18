# Installation and first check

This guide takes you from an empty Python environment to a checked Lean proof.
Choose the path that matches what you already have; Lean Runtime infers the
rest from the current directory and the source itself.

## Requirements

- Python 3.10 or newer;
- Git on `PATH`;
- Linux or macOS.

An existing Elan installation is useful but not required. When the exact
toolchain is already present, Lean Runtime uses its binaries read-only. Missing
toolchains are kept in Lean Runtime's private store.

## Install

Installing in a virtual environment keeps the command isolated from unrelated
Python tools:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install lean-runtime
lean-runtime --version
```

The installed wheel provides one executable: `lean-runtime`.

## Choose your first workflow

=== "Standalone file"

    Create `Main.lean`:

    ```lean
    import Mathlib.Data.Nat.Prime.Basic

    example : Nat.Prime 5 := by decide
    ```

    Check it:

    ```bash
    lean-runtime check Main.lean
    ```

    Outside a Lake project, Lean Runtime analyzes the imports, selects an exact
    catalog environment, and acquires the required verified import closure. The
    second check reuses that environment.

=== "New project"

    ```bash
    lean-runtime new MyProof
    cd MyProof
    lean-runtime check
    lean-runtime build
    ```

    `new` selects a stable cataloged Mathlib/toolchain pair, writes exact Lake
    metadata, and prepares reusable dependencies. Mutating commands show their
    plan before acting; use `--yes` only for non-interactive automation.

=== "Existing Lake project"

    ```bash
    cd ExistingProject
    lean-runtime check
    lean-runtime adopt
    ```

    Checking works before adoption. `adopt` is an optional storage optimization:
    it verifies the pinned manifest and current dependency checkouts, previews
    reuse and recovery, probes a shared graph, and swaps it atomically. It does
    not update `lean-toolchain` or `lake-manifest.json`.

=== "Python"

    ```python
    import lean_runtime as lean

    env = lean.setup(deps=["mathlib@v4.33.0"])
    result = env.check(
        "import Mathlib.Data.Nat.Prime.Basic\n"
        "example : Nat.Prime 5 := by decide\n"
    )
    result.raise_for_error()
    print(result.execution_id)
    ```

    Keep the returned environment and call it repeatedly; setup and acquired
    artifacts are reused.

## Understand what happened

```bash
lean-runtime status Main.lean
lean-runtime storage usage
lean-runtime doctor
```

`status` explains context selection, `storage usage` reports retained content,
and `doctor` checks Git, disk, store health, toolchains, staging areas, and
abandoned workspaces.

For a standalone check, save the exact selected context when you want a durable
artifact:

```bash
lean-runtime check Main.lean --lock-out environment.lock.json
lean-runtime check Main.lean --using environment.lock.json --offline
```

`--offline` is fail-closed: missing toolchains, environments, or sparse import
closures produce an error instead of a network request.

## Where to go next

- [Standalone files](standalone-files.md) — frontmatter, explicit context, watch,
  and matrix checks.
- [Lake projects](local-projects.md) — focused checking, adoption, shared
  dependencies, and updates.
- [Python API](python-api.md) — batch, async, cancellation, multi-file, and
  interactive use.
- [Trust and limitations](trust-and-limitations.md) — the exact integrity and
  execution boundary.

??? question "Something failed on the first run?"

    Run `lean-runtime doctor`, then repeat with `--verbose`. A completed Lean
    rejection exits 1; an invalid invocation or infrastructure failure exits 2.
    The [CLI reference](cli.md#exit-status) lists all public exit classes.
