# Lean Runtime

Run Lean proofs from Python or a single `.lean` file—without creating a throwaway
Lake project or rebuilding the same dependencies on every machine.

Lean Runtime discovers the exact Lean environment a project needs and reuses a
downloadable copy when one is available. It returns structured Lean results
with a record of the toolchain and dependencies that were actually used.

> **Status:** V1 beta. The local backend runs trusted Lean, Lake, and package code;
> it is an orchestration boundary, not a security sandbox.

## Install

```bash
python -m pip install lean-runtime
```

Lean Runtime manages its own Elan installation on macOS and Linux. Windows
currently requires `LEAN_RUNTIME_ELAN`.

## Run one Lean file

Inside an existing pinned Lake project, just pass the file:

```bash
lean-run MyProject/Main.lean
```

Standalone files do not need a throwaway Lake project or dependency declaration:

```lean
import Mathlib

example : 2 + 2 = 4 := by norm_num
```

```bash
lean-run Main.lean
```

When no explicit context or pinned Lake project exists, `lean-run` analyzes imports,
ranks a bounded set of exact environments from its bundled catalog, and asks Lean to
check each candidate. The successful exact lock is retained by Runtime. Pin it for
portable reuse whenever desired:

```bash
lean-run Main.lean --lock-out environment.lock.json
lean-run Main.lean --lock environment.lock.json
```

The bundled catalog covers Mathlib v4.30.0 through v4.33.0 and matching LeanCert
releases, plus core Lean v4.32.2. Runtime first tries its local store and downloadable
environment libraries, then builds the exact source environment when necessary. Use
`--no-source-build` to forbid that potentially large fallback or `--offline` to use
retained environments only.

Explicit frontmatter remains available when the desired context is already known:

```lean
-- /// lean-runtime
-- requires = ["mathlib@v4.33.0"]
-- ///

import Mathlib
```

The same context can be supplied from the command line:

```bash
lean-run Main.lean --with mathlib@v4.33.0
```

Create an exact lock from an explicit dependency for CI without changing the file:

```bash
lean-run Main.lean --with mathlib@v4.33.0 \
  --lock-out environment.lock.json
lean-run Main.lean --lock environment.lock.json
```

## Python

Configure an environment once, then use it repeatedly:

```python
import lean_runtime as lean

env = lean.setup(["mathlib@v4.33.0"])

result = env.check(
    """
    import Mathlib
    example : 2 + 2 = 4 := by norm_num
    """
)
result.raise_for_error()
```

Rejected proofs carry parsed diagnostics:

```python
result = env.check(broken_proof)

for error in result.errors:
    print(error.file, error.line, error.message)

result.raise_for_error()  # raises LeanCheckError with the same detail
```

Core-only work does not need a dependency:

```python
core = lean.setup(toolchain="v4.32.2")
core.check("example : 2 + 2 = 4 := rfl").raise_for_error()
```

Batch and asyncio APIs reuse that prepared environment:

```python
results = env.check_many(generated_proofs, concurrency=8)
results = await env.check_many_async(generated_proofs, concurrency=20)
```

Local projects use the same setup pattern while retaining mutable-project
semantics:

```python
project = lean.setup(project="./my-project")
result = project.check_file("./my-project/MyProject/Main.lean")
```

One-shot helpers are available when setup reuse is unnecessary:

```python
result = lean.check(source, deps=["mathlib@v4.33.0"])
result = lean.check_file("./my-project/MyProject/Main.lean")
```

When you need evidence rather than extra setup, the operations CLI can verify, explain,
compare, and measure the same exact contexts:

```bash
lean-runtime verify research-stack --offline
lean-runtime compare previous.lock.json environment.lock.json
lean-runtime profile research-stack Main.lean --repeat 5
lean-runtime matrix compatibility.toml Main.lean
```

Use `lean-run Main.lean --explain` to inspect context routing without executing Lean, and
`--timings` to expose preparation versus execution time. Successful ordinary checks remain
one concise line.

Friendly references remain exact: use `mathlib@VERSION`,
`leancert@VERSION`, `owner/repository@REVISION`, or the explicit
`github:owner/repository@REVISION` form. Bare floating package names are never
accepted.

## Share environments

A **project** is your ordinary Lake repository. Its **environment** is the exact
Lean version, dependencies, and build configuration needed to use it. A
**downloadable environment** is a ready-to-use copy that collaborators and CI
can fetch instead of rebuilding Mathlib.

Environment libraries may be public or private. For example:

```bash
lean-runtime --library ghcr.io/owner/lean-environments download environment.lock.json
lean-runtime build-and-publish environment.lock.json \
  --publish-to ghcr.io/owner/lean-environments
```

To publish an existing clean, pushed GitHub Lean project, inspect it and
generate the maintained multi-platform workflow:

```bash
lean-runtime project inspect . --module MyProject
lean-runtime project init-publish . \
  --module MyProject \
  --library ghcr.io/owner/my-project-environments
```

The workflow builds and verifies Linux and macOS environments, finalizes the
index atomically, then checks clean consumers. See
[Publishing a Lean project](https://alerad.github.io/lean-runtime/project-publishing/).

For an already-built executable, Lean Runtime can also create a verified
**ready-to-run program**. It opens without rebuilding the project, can be saved
as a portable copy, and can be shared through a public or private program
library. See [Ready-to-run programs](https://github.com/alerad/lean-runtime/blob/main/docs/ready-programs.md).

## Technical details

The simple API is backed by exact Git commits and trees, Lake-resolved locks,
platform-aware content-addressed environments, atomic cross-process builds,
downloadable environment reuse, replayable provenance, verification, and trusted
publishers. The libraries use OCI-compatible storage internally, but users do
not need Docker or container concepts. Advanced protocol details remain in the
architecture documentation.

## Documentation

- [Getting started](https://github.com/alerad/lean-runtime/blob/main/docs/getting-started.md)
- [Python API](https://github.com/alerad/lean-runtime/blob/main/docs/python-api.md)
- [`lean-run` and operations CLI](https://github.com/alerad/lean-runtime/blob/main/docs/cli.md)
- [Managed environments](https://github.com/alerad/lean-runtime/blob/main/docs/environments.md)
- [Local Lake projects](https://github.com/alerad/lean-runtime/blob/main/docs/local-projects.md)
- [Publishing a Lean project](https://github.com/alerad/lean-runtime/blob/main/docs/project-publishing.md)
- [Portable copies and environment libraries](https://github.com/alerad/lean-runtime/blob/main/docs/portable-copies.md)
- [Ready-to-run programs](https://github.com/alerad/lean-runtime/blob/main/docs/ready-programs.md)
- [Architecture](https://github.com/alerad/lean-runtime/blob/main/docs/architecture.md)
- [Trust and limitations](https://github.com/alerad/lean-runtime/blob/main/docs/trust-and-limitations.md)
- [V1 release case study](https://github.com/alerad/lean-runtime/blob/main/docs/case-study-v1.md)

## License

Apache License 2.0.
