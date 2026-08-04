# Lean Runtime

Run Lean proofs from Python or a single `.lean` file—without creating a throwaway
Lake project or rebuilding the same dependencies on every machine.

Lean Runtime discovers or resolves the environment, checks a global OCI cache,
and returns structured Lean results with exact provenance.

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

For a portable standalone file, declare exact dependencies in TOML frontmatter:

```lean
-- /// lean-runtime
-- requires = ["mathlib@v4.32.2"]
-- ///

import Mathlib

example : 2 + 2 = 4 := by norm_num
```

```bash
lean-run Main.lean
```

The same context can be supplied from the command line:

```bash
lean-run Main.lean --with mathlib@v4.32.2
```

Create an exact lock for CI without changing the file:

```bash
lean-run Main.lean --with mathlib@v4.32.2 \
  --lock-out environment.lock.json
lean-run Main.lean --lock environment.lock.json
```

## Python

Configure an environment once, then use it repeatedly:

```python
import lean_runtime as lean

env = lean.setup(["mathlib@v4.32.2"])

result = env.check(
    """
    import Mathlib
    example : 2 + 2 = 4 := by norm_num
    """
)
result.raise_for_error()
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
result = lean.check(source, deps=["mathlib@v4.32.2"])
result = lean.check_file("./my-project/MyProject/Main.lean")
```

When you need evidence rather than extra setup, the operations CLI can verify, explain,
compare, and measure the same exact contexts:

```bash
lean-runtime verify research-stack --offline
lean-runtime diff previous.lock.json environment.lock.json
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

## Under the hood

The simple API is backed by exact Git commits and trees, Lake-resolved locks,
platform-aware content-addressed environments, atomic cross-process builds,
transparent OCI cache reuse, replayable provenance, verification, and signed
attestations. Advanced users can access all of it through `lean_runtime.Runtime`
and the `lean-runtime` operations CLI.

## Documentation

- [Getting started](https://alerad.github.io/lean-runtime/getting-started/)
- [Python API](https://alerad.github.io/lean-runtime/python-api/)
- [`lean-run` and operations CLI](https://alerad.github.io/lean-runtime/cli/)
- [Managed environments](https://alerad.github.io/lean-runtime/environments/)
- [Local Lake projects](https://alerad.github.io/lean-runtime/local-projects/)
- [Environment bundles and OCI caches](https://alerad.github.io/lean-runtime/bundles/)
- [Architecture](https://alerad.github.io/lean-runtime/architecture/)
- [Trust and limitations](https://alerad.github.io/lean-runtime/trust-and-limitations/)
- [V1 release case study](https://alerad.github.io/lean-runtime/case-study-v1/)

## License

Apache License 2.0.
