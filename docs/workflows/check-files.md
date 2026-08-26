# Check Lean files

`lean-runtime check` accepts files, directories, projects, and standard input. This page covers standalone files.

## Let Lean Runtime propose an environment

```console
lean-runtime check Main.lean
```

For a standalone file without explicit context, Lean Runtime reads its declared
imports and searches the bundled catalog for exact environments that provide
those modules. The search is bounded. Static analysis only proposes candidates;
Lean remains the final authority, and a candidate is accepted only when Lean
accepts the file inside it.

If a scratch file sits below an unrelated Lake project, declarative target
ownership normally keeps it standalone. Use `--standalone` when you want to
override project inference explicitly:

```console
lean-runtime check Scratch.lean --standalone
```

## Preview what check will do

`status` is a dry run of `check`: it shows where the context comes from, which
environments discovery proposes, whether each is already local, and whether a
download is needed. It never runs Lean.

```console
lean-runtime status Main.lean
lean-runtime status Main.lean --probe
lean-runtime status Main.lean --json
```

`--probe` consults the configured libraries to price the first planned
download. Without it, `status` stays offline.

## Record and reuse an exact lock

```console
lean-runtime check Main.lean --write-lock environment.lock.json
lean-runtime check Main.lean --using environment.lock.json
```

To create a lock from an environment specification file instead of a completed check:

```console
lean-runtime env lock environment.toml --output environment.lock.json
```

The positional argument to `env lock` is currently a specification file path.

## Work offline

```console
lean-runtime check Main.lean --using environment.lock.json --offline
```

Offline mode disables remote acquisition. It succeeds only when the selected toolchain and environment content are already retained locally.

## Check repeatedly

```console
lean-runtime check Main.lean --repeat 10
lean-runtime check Main.lean --timings
lean-runtime check Main.lean --verbose
```

`--repeat` produces repeated execution samples. `--timings` reports phase timings. `--verbose` emits runtime events.

## Produce structured output

```console
lean-runtime check Main.lean --json
```

The result is one `lean-runtime.execution/v1` envelope. `data.verdict` is
`accepted`, `rejected`, or `not_run` (a timeout or cancellation, which carries
no verdict). `data.ok` remains true only for `accepted`. The provenance block
records the environment and lock identities, the toolchain, exact package
revisions, and `source_digest` — the digest of the bytes Lean actually checked —
so a result can be re-checked independently later.

A context or acquisition failure also produces exactly one envelope, with
`ok: false` and a populated `errors` list, and a non-zero exit code.

## Select the environment explicitly

Use `--using` only when a particular environment is part of the request:

```console
lean-runtime check Main.lean --using mathlib@v4.33.0
lean-runtime check Main.lean --using environment.lock.json
lean-runtime check Main.lean --using leanprover/lean4:v4.33.0
lean-runtime check some-project/MyProject/Main.lean --using ./some-project
```

Package references resolve a published release. Lock files identify an exact
dependency graph. Toolchain references provide a core Lean environment. A file
checked in a project context must live inside that project.

Frontmatter can carry the same requirement with the source:

```lean
-- /// lean-runtime
-- requires = ["mathlib@v4.33.0"]
-- ///
import Mathlib
```

Supported fields are `requires`, `toolchain`, and `lock`. A lock cannot be
combined with `requires` or `toolchain`.

## Check standard input

Standard input has no filename evidence, so it requires an explicit environment:

```console
echo 'example : True := trivial' | lean-runtime check - --using leanprover/lean4:v4.33.0
```

## Watch a project file

The current watch workflow requires one file inside a pinned Lake project:

```console
cd my-project
lean-runtime watch MyProject/Basic.lean
```

The file is checked again when it changes. Stop the watcher with `Ctrl+C`.
