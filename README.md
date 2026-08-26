# Lean Runtime

**Check the proof. Reuse everything else.**

```bash
python -m pip install lean-runtime
```

Give Lean Runtime an ordinary file:

```lean
-- Main.lean
import Mathlib.Tactic.NormNum

example : 2 + 2 = 4 := by norm_num
```

```console
$ lean-runtime check Main.lean
```

No Lake project, dependency checkout, or toolchain setup required. Lean Runtime
discovers and verifies the exact environment, then keeps it ready for offline
reuse.

[Get started](https://alerad.github.io/lean-runtime/tutorial/) ·
[Read the documentation](https://alerad.github.io/lean-runtime/) ·
[Explore the CLI](https://alerad.github.io/lean-runtime/reference/commands/)

## The model

| Noun | Meaning |
| --- | --- |
| **context** | where a file's requirements come from: `--using`, frontmatter, the owning Lake project, or automatic discovery |
| **environment** | one exact, immutable toolchain + package set |
| **lock** | an environment written down, reusable offline anywhere |
| **verdict** | Lean's answer inside one environment: `accepted` or `rejected` |

Discovery proposes an environment. Only Lean accepts it. `lean-runtime status`
shows the proposal without running anything; `lean-runtime check` produces the
verdict and names the environment it was produced in:

```text
✓ Main.lean accepted in mathlib-v4.33.0 (3.21s)
```

## Start where you are

Create a project:

```bash
lean-runtime new MyProof
cd MyProof
lean-runtime check
lean-runtime build
```

Use an existing pinned Lake project from its directory:

```bash
lean-runtime check
lean-runtime adopt
```

`adopt` verifies the existing exact dependency graph, previews reuse and disk
recovery, asks for confirmation, and swaps dependency links atomically. It does
not change `lean-toolchain` or `lake-manifest.json`. Passing a directory that
contains several projects discovers them automatically:

```bash
lean-runtime adopt ~/research
```

Check a standalone source file:

```bash
lean-runtime check Main.lean
```

The same command uses the nearest pinned Lake project when one exists and
otherwise performs bounded exact-environment discovery. Automatic discovery
uses retained or verified downloadable environments; building a missing
candidate from source requires `--allow-source-build`.

Record the environment only when the result must be reproduced elsewhere:

```bash
lean-runtime check Main.lean --write-lock environment.lock.json
```

A file can instead name its environment in strict comment frontmatter:

```lean
-- /// lean-runtime
-- requires = ["mathlib@v4.33.0"]
-- ///

import Mathlib
example : 2 + 2 = 4 := by norm_num
```

When discovery needs an override, there is one spelling:

```bash
lean-runtime check Main.lean --using mathlib@v4.33.0
lean-runtime check Main.lean --using environment.lock.json
lean-runtime check Main.lean --using research-stack
lean-runtime check Main.lean --using lean:v4.33.0
lean-runtime check Main.lean --using ~/proofs/MyProject
```

Typed `package:`, `lock:`, `env:`, `toolchain:`, and `project:` prefixes resolve
rare ambiguities. A project context applies to files inside that project's
root. Persistent store, registry, and trust policy belongs in
environment configuration rather than everyday command lines.

## Commands

The normal surface is deliberately small:

```text
new NAME         create a project
adopt [PATH]     share dependencies from existing project(s)
check [PATH…]    check a project, directory, source file, or stdin (-)
watch FILE       re-check on save
build [TARGET]   build the current project
update           preview and apply a safe project update
publish          configure verified project publication
status [PATH]    dry run of check: where the environment comes from, what it costs
verify SUBJECT   verify an exact artifact
doctor           diagnose and offer safe repairs
clean            preview and reclaim unused storage
replay CAPTURE   replay an execution capture
completion SHELL generate shell completion
```

Project commands use the current directory when no path is supplied. Every
command that changes a project, deletes local content, or publishes to a remote
shows its plan and asks before changing anything; automation passes `--yes`,
and inspection-only calls pass `--dry-run`.

Persistent registry, availability, store, and publisher-trust policy belongs
in `~/.config/lean-runtime/config.toml`; the nearest project's
`lean-runtime.toml` can override it. Daily commands therefore normally need no
configuration flags.

Exact and operator workflows live under noun namespaces:

```text
env       list · info · lock · acquire · diff · export · import
project   info · scan · share · unshare · lock · export
program   create · run · info · acquire · export · import · publish
toolchain list · info · install · optimize
storage   usage · verify
catalog   catalog maintenance
```

There are no v3 command aliases. `run`, `init`, `prepare`, `open`, `download`,
`environments`, `inspect`, `compare`, `copy`, `finalize`, `lean-run`, and
`lean-runtime-catalog` were removed in 4.0.

## Existing Elan installations

Lean Runtime automatically reuses an exact compatible toolchain already
installed by the user's Elan. This access is read-only: it never changes the
user's default, installs into the user's Elan home, or removes user toolchains.
Missing toolchains and downloadable slim checking runtimes remain isolated in
Lean Runtime's private store. `lean-runtime status` and `doctor` expose the
choice when it matters.

## Exact environments

```bash
lean-runtime env lock environment.toml --output environment.lock.json
lean-runtime env acquire environment.lock.json --name research-stack
lean-runtime env info research-stack
lean-runtime env diff previous.lock.json environment.lock.json
lean-runtime env export research-stack --output research-stack.lean-environment
lean-runtime env import research-stack.lean-environment --name imported-stack
```

Locks are canonical and content-addressed. Full environments preserve source;
downloaded sparse capsules project only verified import closures and keep the
same environment identity as their projection grows.

## Python

```python
import lean_runtime as lean

env = lean.setup(deps=["mathlib@v4.33.0"])
result = env.check("import Mathlib\nexample : 2 + 2 = 4 := by norm_num\n")
result.raise_for_error()
```

The Python API retains the explicit `Runtime`, `EnvironmentSpec`,
`EnvironmentLock`, project, capture, program, cancellation, and verification
interfaces for infrastructure code.

## Guarantees and limits

- Exact Git commits, trees, locks, toolchains, platform identities, and artifact
  digests are verified before an environment becomes ready.
- Acquisitions and project sharing are staged, probed, and published atomically.
- User project metadata and user Elan state are not silently rewritten.
- The local execution backend enforces supported resource limits but is not a
  network sandbox; unsupported isolation requests fail explicitly.
- A Lean rejection is a normal verdict and exits 1. Invalid or infrastructure
  invocations exit 2 and carry no verdict. Publication failures retain their
  documented classified exit statuses.

Documentation lives at
[alerad.github.io/lean-runtime](https://alerad.github.io/lean-runtime/).
