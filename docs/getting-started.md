# Getting started

## Install

```bash
python -m pip install lean-runtime
```

On macOS and Linux, Lean Runtime first uses a published slim check toolchain when
one is available and otherwise installs the requested Lean version through its
private, checksum-verified Elan installation. It does not change the user's
default toolchain or shell profile. Windows currently requires an existing Elan
executable through `LEAN_RUNTIME_ELAN`.

## Start a Lake project

Create a standard Lake library with the newest cataloged Mathlib release and
shared exact dependencies:

```bash
lean-runtime init MyProof
cd MyProof
lean-runtime check MyProof/Basic.lean
lean-runtime build
```

By default, `init` writes an `AGENTS.md` explaining the project workflow and
shared-dependency safety rules to coding agents. Pass `--no-agents` to omit it.
An existing file is preserved.

Use `--mathlib 4.33.0` to select that release explicitly, or `--core` for a
core-only library. The root project remains an ordinary mutable Lake project;
its dependency sources and build artifacts are reused by other projects with
the same exact graph.

Before doing any work, `init --plan` reports the exact release, local reuse, and
known download size. `--max-download 500MiB` refuses a larger transfer and
`--offline` requires a matching local graph. Initialization is transactional:
the target appears only after Lake and the shared dependency graph both verify.
Offline plans perform no registry lookup; they return a blocked plan and a
nonzero status when the required exact local graph is absent.
Project development requires one full Lake-capable Lean toolchain per Lean
version. When it is absent, the plan says its Elan download size is unknown;
`--offline` and `--max-download` fail closed instead of silently installing it.

The target may be a missing directory, an empty directory, or an otherwise
empty Git repository root. In the Git case, `init` preserves the existing HEAD,
index, remotes, and worktree metadata. It also preserves an existing
`AGENTS.md`; any other non-project contents are rejected by both `--plan` and
execution rather than overwritten.
If the target already exists, initialization keeps that directory inode alive;
the shell that runs `lean-runtime init .` can immediately continue using it.
The directory name normally supplies the Lake package/root module name. Override
it when capitalization matters, for example:

```bash
lean-runtime init . --name IntegralFramework
```

The same command can be rerun safely after initialization; a different name is
rejected rather than silently renaming an existing project.

Dependency upgrades are explicit:

```bash
lean-runtime update --plan
lean-runtime update
```

The update preview names both exact Mathlib revisions and toolchains. In a
terminal, the second command asks for confirmation; use `--yes` in automation.

For one existing pinned Lake project, preserve and adopt its current exact graph:

```bash
lean-runtime init .
lean-runtime scan ~/research
```

`scan` only records exact local graphs as possible future zero-download seeds.
Advanced bulk migration remains available through `attach`. See [Local Lake
projects](local-projects.md) for detachment, storage estimates, and the complete
safety model.

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
