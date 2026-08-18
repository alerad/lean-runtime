# Get started

Two minutes from `pip install` to a checked proof.

## What you need

- Python 3.10+
- Git
- Linux or macOS

That's it — Lean itself is handled for you. If you already have Elan
installed, Lean Runtime reuses its toolchains read-only and never touches
your defaults.

## Install

```bash
python -m pip install lean-runtime
lean-runtime --version
```

(Prefer a virtual environment? `python -m venv .venv && source
.venv/bin/activate` first — everything else is the same.)

## Your first proof

=== "Standalone file"

    In any directory, create `Main.lean`:

    ```lean
    import Mathlib.Data.Nat.Prime.Basic

    example : Nat.Prime 5 := by decide
    ```

    Check it:

    ```bash
    lean-runtime check Main.lean
    ```

    No project needed. Lean Runtime reads the import, picks a matching
    Mathlib release, downloads what the file uses, and runs Lean. The
    second check reuses all of it and is fast.

=== "New project"

    ```bash
    lean-runtime new MyProof
    cd MyProof
    lean-runtime check
    ```

    `new` creates a project pinned to a known-good Mathlib and toolchain
    pair. Commands that change things show their plan and ask first;
    pass `--yes` in scripts.

=== "Existing Lake project"

    ```bash
    cd ExistingProject
    lean-runtime check
    ```

    That's all — checking works with your project exactly as it is.
    Optionally, `lean-runtime adopt` deduplicates dependency storage
    across your projects. It never touches `lean-toolchain` or
    `lake-manifest.json`, and it rolls back automatically if anything
    fails.

=== "Python"

    ```python
    import lean_runtime as lean

    env = lean.setup(deps=["mathlib@v4.33.0"])
    result = env.check("import Mathlib.Data.Nat.Prime.Basic\nexample : Nat.Prime 5 := by decide\n")
    result.raise_for_error()
    ```

    Keep `env` around and call it repeatedly — setup happens once.

## See what it did

```bash
lean-runtime status Main.lean   # which environment was picked, and why
lean-runtime storage usage      # what's on disk
lean-runtime doctor             # health check if anything seems off
```

## Take it offline

Save the exact environment your check used, then reuse it with no network
at all:

```bash
lean-runtime check Main.lean --lock-out environment.lock.json
lean-runtime check Main.lean --using environment.lock.json --offline
```

`--offline` never quietly reaches for the network — anything missing is a
clear error instead.

## Where next

- [Check a single file](standalone-files.md) — pin versions, watch mode,
  checking against several Mathlib releases at once.
- [Lake projects](local-projects.md) — focused checks, shared
  dependencies, safe updates.
- [Python API](python-api.md) — batch, async, cancellation, and
  interactive sessions.

??? question "Something failed on the first run?"

    Run `lean-runtime doctor`, then retry with `--verbose`. Exit code 1
    means Lean rejected the proof; exit code 2 means something else went
    wrong (bad invocation, network, disk). The
    [CLI reference](cli.md#exit-status) lists all exit codes.
