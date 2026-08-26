# Start here

This guide checks one core Lean file and one file that imports Mathlib. It then records an exact environment for later reuse.

## The model in one paragraph

Every check runs inside one **environment**: an exact, immutable toolchain plus
package set. Where that environment comes from is the **context** — an explicit
`--using`, frontmatter in the file, the Lake project that owns the file, or
automatic discovery from the file's imports. A **lock** is an environment written
down so it can be reused anywhere, including offline. The **verdict** is Lean's
answer inside that environment: `accepted` or `rejected`.

Discovery proposes an environment. Only Lean accepts it.

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

Lean Runtime proposes a catalog environment from the file's (absent) imports and
runs Lean inside it. The last line names the verdict and the environment it was
produced in:

```text
✓ Basic.lean accepted in core-v4.32.2 (0.41s)
```

## Check a file that imports Mathlib

Create `Primes.lean`:

```lean
import Mathlib.Data.Nat.Prime.Infinite

example : ∀ n : ℕ, ∃ p, n ≤ p ∧ p.Prime :=
  Nat.exists_infinite_primes
```

Before running Lean, ask what `check` is going to do:

```console
lean-runtime status Primes.lean
```

```text
Primes.lean — standalone file (no owning Lake project)
  Context      automatic discovery · proposed from imports; Lean has not run
  Imports      Mathlib.Data.Nat.Prime.Infinite
  Will try     mathlib-v4.33.1        environment not local · toolchain not installed
               mathlib-v4.33.0        environment not local · toolchain not installed
               …
  Download     required for mathlib-v4.33.1; add --probe to see the size

Next: lean-runtime check Primes.lean compiles the file and reports its verdict.
```

`status` is a dry run. It never runs Lean and never downloads anything; add
`--probe` to price the first download against the configured libraries. Then
check the file:

```console
lean-runtime check Primes.lean
```

Discovery tries the proposed environments in order, within a bounded policy. A
candidate succeeds only when Lean accepts the source inside it. The first run
may install a toolchain and acquire environment content; later checks reuse
retained content.

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

## Override selection

Most checks should rely on discovery. Use `--using` only when a particular
release or environment is part of the request:

```console
lean-runtime check Primes.lean --using mathlib@v4.33.0
```

See [Context selection](concepts/context-selection.md) for frontmatter, project
precedence, and other explicit context forms.

## Interpret the result

A rejection is a normal result: Lean ran to completion inside one environment
and said no. Exit code `2` means something different — no environment could be
obtained, or Lean could not be run — and carries no verdict.

| Exit code | Verdict | Meaning |
| --- | --- | --- |
| `0` | `accepted` | Lean accepted the source in the reported environment. |
| `1` | `rejected` | Lean ran and rejected the source. |
| `2` | none | Invocation, context, acquisition, or configuration failed, or a resource limit such as `--timeout` was hit. |
| `130` | none | The operation was interrupted. |

`--json` carries the same distinction as `data.verdict` (`accepted`,
`rejected`, or `not_run`) alongside the environment and lock identities and the
digest of the source that ran. Use `--verbose` for the runtime event stream.

## Continue

- [Check Lean files](workflows/check-files.md)
- [Work with Lake projects](workflows/lake-projects.md)
- [Understand context selection](concepts/context-selection.md)
- [Browse the command summary](reference/commands.md)
- [Use the Python API](reference/python-api.md)
