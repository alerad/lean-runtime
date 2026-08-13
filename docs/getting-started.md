# Getting started

## Install

```bash
python -m pip install lean-runtime
```

On macOS and Linux, Lean Runtime installs requested Lean versions through its
private, checksum-verified Elan installation. It does not change the user's
default toolchain or shell profile. Windows currently requires an existing Elan
executable through `LEAN_RUNTIME_ELAN`.

## Run a file

For a file inside a pinned Lake project:

```bash
lean-run MyProject/Main.lean
```

Lean Runtime walks upward to the nearest `lakefile.toml` or `lakefile.lean` with
a `lean-toolchain`, then checks the actual project-relative path.

Standalone files can be checked directly:

```lean
import Mathlib
example : 2 + 2 = 4 := by norm_num
```

```bash
lean-run Main.lean
```

`lean-run` uses its bundled catalog to rank a bounded set of exact environments,
then lets Lean determine which candidate accepts the source. Use
`--lock-out environment.lock.json` to pin the successful environment.

Strict TOML frontmatter remains available when the context is already known:

```lean
-- /// lean-runtime
-- requires = ["mathlib@v4.33.0"]
-- ///

import Mathlib
```

Dependencies may instead be supplied without editing the file:

```bash
lean-run Main.lean --with mathlib@v4.33.0
```

Friendly references require a revision and compile to exact Git identities:

- `mathlib@v4.33.0`
- `leancert@v4.33.0`
- `owner/repository@tag-or-commit`
- `github:owner/repository@tag-or-commit`

Bare floating aliases are rejected.

## Python setup

```python
import lean_runtime as lean

environment = lean.setup(["mathlib@v4.33.0"])
result = environment.check("import Mathlib\nexample : 2 + 2 = 4 := by norm_num")
result.raise_for_error()
```

`setup()` resolves and ensures the environment once. Further checks reuse that
handle, including batch and asynchronous calls.

The same entry point opens local projects, exact locks, previously named
environments, and core-only toolchains:

```python
project = lean.setup(project="./my-project")
locked = lean.setup(lock="environment.lock.json")
existing = lean.setup(environment="research-stack")
core = lean.setup(toolchain="v4.32.2")
```

Exactly one context must be supplied.

Rejected checks expose parsed diagnostics directly, with `file` naming your
logical input:

```python
result = environment.check(broken_proof)
for error in result.errors:
    print(error.file, error.line, error.message)
```

## Lock for CI

Resolve a friendly dependency declaration and retain its exact graph:

```bash
lean-run Main.lean --with mathlib@v4.32.2 \
  --lock-out environment.lock.json
```

Subsequent runs can skip dependency resolution:

```bash
lean-run Main.lean --lock environment.lock.json
```

Frontmatter may reference a lock relative to the Lean file:

```lean
-- /// lean-runtime
-- lock = "environment.lock.json"
-- ///
```

See [Standalone Lean files](standalone-files.md) for routing and validation
rules, or [Python API](python-api.md) for the explicit advanced API.
