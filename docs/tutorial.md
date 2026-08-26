# Start here

This guide checks one core Lean file and one file that imports Mathlib. It then records an exact environment for later reuse.

## Requirements

- Python 3.10 or newer
- Git
- macOS or Linux for automatic Elan bootstrap
- On Windows, an existing Elan installation available on `PATH` (or identified
  explicitly by `LEAN_RUNTIME_ELAN`)

## Install Lean Runtime

```console
python -m pip install lean-runtime
lean-runtime --version
```

`lean-runtime doctor` reports the runtime home, platform support, toolchain manager, and relevant configuration.

## Check a core Lean file

Create `Basic.lean`:

```lean
example : 1 + 1 = 2 := by decide
```

Check it directly:

```console
lean-runtime check Basic.lean
```

Lean Runtime selects a plausible catalog context and runs Lean. Exit code `0`
means Lean accepted the file in the reported context.

## Check a file that imports Mathlib

Create `Primes.lean`:

```lean
import Mathlib.Data.Nat.Prime.Infinite

example : ∀ n : ℕ, ∃ p, n ≤ p ∧ p.Prime :=
  Nat.exists_infinite_primes
```

Run the check without specifying a context:

```console
lean-runtime check Primes.lean
```

For a standalone file, Lean Runtime uses imports as evidence for automatic
discovery. It tries exact catalog environments within a bounded policy. A
candidate succeeds only when Lean accepts the source in that environment.

The first run may install a toolchain and acquire environment content. Later checks reuse retained content.

## Record an exact lock

Write the environment used by a successful check:

```console
lean-runtime check Primes.lean --write-lock primes.lock.json
```

Use the lock on a later run:

```console
lean-runtime check Primes.lean --using primes.lock.json
```

After the required toolchain and environment content are available locally, the same check can prohibit network access:

```console
lean-runtime check Primes.lean --using primes.lock.json --offline
```

Offline mode does not acquire missing remote content. Missing requirements produce an infrastructure failure rather than a Lean rejection.

## Troubleshoot or override selection

`status` previews routing without running Lean:

```console
lean-runtime status Primes.lean
```

Most checks should rely on discovery. Use `--using` only when a particular
release or context is part of the request:

```console
lean-runtime check Primes.lean --using mathlib@v4.33.0
```

See [Context selection](concepts/context-selection.md) for frontmatter, project
precedence, and other explicit context forms.

## Interpret the result

| Exit code | Meaning |
| --- | --- |
| `0` | Lean accepted the source. |
| `1` | Lean ran and rejected the source. |
| `2` | Invocation, context, acquisition, or configuration failed, or a resource limit such as `--timeout` was hit. |
| `130` | The operation was interrupted. |

Use `--json` for structured results and `--verbose` for the runtime event stream.

## Continue

- [Check Lean files](workflows/check-files.md)
- [Work with Lake projects](workflows/lake-projects.md)
- [Understand context selection](concepts/context-selection.md)
- [Browse the command summary](reference/commands.md)
- [Use the Python API](reference/python-api.md)
