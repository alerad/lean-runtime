# Lean Runtime

Lean Runtime compiles declarative specifications into content-addressed Lean
execution environments.

```text
environment specification + Lean source + execution policy
                              ↓
kernel-checked result + exact environment and execution provenance
```

It does not replace Elan or Lake. Elan remains authoritative for toolchains and
Lake remains authoritative for dependency resolution and builds. Lean Runtime
adds immutable identities, lifecycle management, reuse, structured Python
results, and replayable provenance above them.

> **Status:** `0.2` alpha. Exact Git environments and trusted local execution
> are implemented. Local execution is an orchestration boundary, not a security
> sandbox.

Full guides, API examples, architecture, and the trust model live in the
[documentation](https://alerad.github.io/lean-runtime/).

## Installation

```bash
python -m pip install .
```

For development:

```bash
python -m pip install -e '.[dev]'
```

Users do not need a separately managed Lean installation. On macOS and Linux,
Lean Runtime bootstraps a private Elan installation and installs requested Lean
versions into its own cache. Windows currently requires `LEAN_RUNTIME_ELAN`.

## Python API

```python
from lean_runtime import EnvironmentSpec, GitPackage, Runtime

runtime = Runtime()

spec = EnvironmentSpec(
    toolchain="leanprover/lean4:v4.32.2",
    packages=(
        GitPackage(
            name="mathlib",
            url="https://github.com/leanprover-community/mathlib4.git",
            rev="905b95818eb32af7874a58b427f50c1711a5e96c",
            root_module="Mathlib",
            artifact_command=("lake", "exe", "cache", "get"),
        ),
    ),
)

# Resolution is deliberately separate from materialization.
lock = runtime.resolve(spec)
environment = runtime.ensure(lock, name="mathlib-4.32.2")

result = environment.check(
    """
    import Mathlib

    example : 2 + 2 = 4 := by norm_num
    """
)

assert result.ok
print(result.environment_id)
print(result.execution_id)
print(result.provenance.request_digest)
print(result.provenance.packages)
```

`runtime.open()` performs no resolution and needs no network access:

```python
same_environment = Runtime().open(result.environment_id)
replayed = same_environment.check("import Mathlib\nexample : True := by trivial")
```

The convenience form compiles and reuses the environment automatically:

```python
result = runtime.check(source, environment=spec)
```

## Exact package policy

The first lock schema accepts only exact Git commits:

```python
GitPackage(
    name="sample",
    url="https://github.com/example/sample.git",
    rev="0123456789abcdef0123456789abcdef01234567",
    root_module="Sample",
)
```

Tags, branches, semantic versions, editable dependencies, and path packages are
intentionally not part of the initial model. Lake may resolve transitive Git
inputs, but every package in the resulting lock records its full commit and Git
tree identity.

`root_module` tells the generated environment root what to import so the
package's Lean artifacts are built. `artifact_command` is an optional explicit
package-supported hydration step; it is useful for Mathlib's cache command
without introducing a premature artifact-provider framework.

Artifact commands run from the generated root workspace. Locks, packages, and
artifact commands must be trusted; schema validation is not a security sandbox.

## CLI

An environment specification can be JSON or TOML. See
[examples/mathlib.toml](examples/mathlib.toml).

```bash
lean-runtime resolve environment.toml --output environment.lock.json
lean-runtime ensure environment.lock.json --name research-stack
lean-runtime check research-stack Main.lean --json
lean-runtime inspect research-stack
lean-runtime replay result.execution.json --json
lean-runtime gc                         # dry-run
lean-runtime gc --execute              # removes old, unnamed environments
```

Raw execution remains available for existing projects and core-only snippets:

```bash
lean-runtime raw-check Main.lean --toolchain 4.32.0
lean-runtime raw-check Main.lean --project ./existing-project
lean-runtime project-build ./existing-project MyLibrary
```

## Execution policy

```python
from lean_runtime import ExecutionPolicy

policy = ExecutionPolicy(
    timeout_seconds=30,
    max_output_bytes=1_000_000,
    memory_mb=2048,
    cpu_seconds=20,
)

result = environment.check(source, policy=policy)
print(result.provenance.enforced_policy_fields)
```

The local Unix backend enforces timeout, bounded captured output, address-space
and CPU limits. It cannot enforce network isolation and rejects
`network="disabled"` rather than claiming otherwise. Future container and
remote backends can implement stronger policies without changing environment
semantics.

Checks can be cancelled or batched:

```python
job = environment.start_check(source)
job.cancel()
result = job.result()

results = environment.check_many(sources, concurrency=8)
```

## Captures

The first capsule representation is intentionally a canonical JSON manifest,
not a bespoke archive:

```python
capture = environment.capture(source, expected_ok=True)
capture.write("result.execution.json")
```

It contains the complete environment lock, input files, policy, operation, and
optional expected outcome. `runtime.replay_capture(...)` or
`lean-runtime replay` can acquire the exact locked sources and recreate the
environment without invoking dependency resolution. Source/binary archives,
signatures, and attestations are deferred until their trust model is clear.

## Store

The default store is `~/Library/Caches/lean-runtime` on macOS and
`${XDG_CACHE_HOME:-~/.cache}/lean-runtime` on Linux. Set `LEAN_RUNTIME_HOME` to
override it.

```text
lean-runtime/
  elan/          private toolchains
  sources/git/   immutable exact source snapshots
  locks/         portable Lake-backed locks
  environments/  platform-specific published builds
  names/         mutable aliases to immutable identities
  executions/    result/provenance records
  jobs/          disposable writable execution instances
```

See [Architecture](docs/architecture.md) for identities, publication rules,
offline behavior, and trust boundaries.

## Security

Lean files, dependency Lake configurations, custom targets, native extensions,
and artifact commands are trusted code. Content addressing provides identity
and reuse; it is not a sandbox. Do not build adversarial packages with the
local backend.

## License

Apache License 2.0.
