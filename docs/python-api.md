# Python API

## Prepared environments

The usual entry point prepares one context and returns either an immutable
`Environment` or mutable `ProjectEnvironment`:

```python
import lean_runtime as lean

environment = lean.setup(["mathlib@v4.32.2"])
project = lean.setup(project="./my-project")
locked = lean.setup(lock="environment.lock.json")
existing = lean.setup(environment="research-stack")
core = lean.setup(toolchain="v4.32.2")
```

Exactly one of `deps`, `project`, `lock`, `environment`, or a bare
`toolchain` is required. A bare `toolchain` prepares the core-only
environment for that Lean release; combined with `deps` or `project` it
remains an override. The
default `Runtime` is created lazily on the first operation; importing
`lean_runtime` has no filesystem or network side effects. Supply
`runtime=Runtime(...)` to any façade function for explicit configuration.

When an exact library has a current check capsule, `setup()` may initially
materialize only its metadata. Each `check()` extends the projection with the
source's exact transitive import closure. Reusing the `Environment` also reuses
the verified CAS artifacts; its lock and environment identity do not change.

Optional editor indexes are explicit:

```python
environment.require_capabilities(
    ["editor"],
    imports=["Mathlib.Data.Nat.Prime.Basic"],
)
```

Native and development capabilities require a full built environment and are
rejected by check capsules with an actionable error.

One-shot helpers use the same routing:

```python
result = lean.check(source, deps=["mathlib@v4.32.2"])
result = lean.check_file("./my-project/MyProject/Main.lean")
result = lean.replay("execution.capture.json")
```

## Environment discovery

The `lean-run` command performs discovery automatically for context-free
standalone files. Applications can use the same bounded planner and
compiler-authoritative search explicitly:

```python
from lean_runtime.discovery import Discovery, default_catalog

discovery = Discovery(catalog=default_catalog())
result = discovery.discover_and_check("""
import Mathlib
example : 2 + 2 = 4 := by norm_num
""")
result.raise_for_error()

print(result.lock_id)
print(result.environment_id)
```

Every successful result contains the exact `EnvironmentLock` accepted by Lean.
Pass that lock to ordinary Runtime operations to bypass discovery on future
runs. Planning metadata narrows candidates but never asserts compatibility.

## Results

Results remain inspectable values. Failed checks expose their parsed
diagnostics directly:

```python
result = environment.check(source)

for error in result.errors:
    print(error.file, error.line, error.message)

if result.first_error is not None:
    print(result.first_error.location)
```

`errors` and `warnings` are severity-filtered views of `diagnostics`;
`first_error` returns the first error or `None`. Diagnostic `file` values name
the caller's logical input (for example `Main.lean`); raw compiler output
remains authoritative on `stdout`/`stderr` and may contain physical sandbox
paths. Scripts that prefer exceptions can use:

```python
result.raise_for_error()
```

Rejection raises `LeanCheckError`, whose `result` attribute retains diagnostics,
stdout, stderr, environment and execution identities, and complete provenance.

## Explicit Runtime API

`Runtime` exposes resolution, stores, policies, publishing, and lifecycle
operations directly:

```python
runtime.prepare(spec)  # -> exact EnvironmentLock
runtime.open_exact(lock, name="friendly-name")  # -> Environment
runtime.environment("friendly-name")  # reopen a ready environment, offline
runtime.check(source, environment=spec)  # convenience path
runtime.check(source, packages=["github:owner/repository@v1.0.0"])
runtime.replay_capture("run.json")  # replay a capture
runtime.clean(dry_run=True)  # inspect reclaimable environments (advanced API)
```

Configure environment libraries and publish ready-to-use environments:

```python
runtime = Runtime(
    libraries=["ghcr.io/alerad/lean-environments"],
    availability="auto",
)
environment = runtime.open_exact(lock)
published = runtime.publish_environment(
    environment.id,
    "ghcr.io/alerad/lean-environments",
    tags=["v4.32.2.4"],
)
```

Package-reference discovery is also exposed in separable stages:

```python
spec = runtime.spec_from_references(["github:alerad/leancert@v4.32.2.4"])
lock = runtime.prepare_references(["github:alerad/leancert@v4.32.2.4"])
environment = runtime.open_references(
    ["github:alerad/leancert@v4.32.2.4"],
    name="leancert-4.32.2.4",
)
```

Discovery requires a root `lean-toolchain`, a root `lakefile.toml`, and at
least one `[[lean_lib]]`. The convenience specification contains the resolved
commit, so all downstream identities retain the same exact semantics as a
manually authored `EnvironmentSpec`.

Core-only snippets can select a toolchain directly. Existing Lake projects have
a distinct mutable handle:

```python
runtime.check(source, toolchain="4.32.2")
project = runtime.project("./existing-project")
project.build(("MyLibrary",))
project.check_file("./existing-project/MyLibrary/Main.lean")
project.check(source)
```

`runtime.project()` also accepts a contained file and discovers the nearest
pinned Lake root. `runtime.check_file(path)` performs this discovery
automatically when no managed environment, packages, or toolchain are supplied.
See [Local Lake projects](local-projects.md).

## Environment

```python
result = environment.check(source)
result = environment.check_files(files, entrypoint="Main.lean")
results = environment.check_many(sources, concurrency=8)
build = environment.build(("RuntimeEnvironment",))
command = environment.execute(["lake", "exe", "my_tool", "--flag"])
info = environment.inspect()
capture = environment.capture(source, expected_ok=True)
```

Asynchronous cancellation uses a background job:

```python
job = environment.start_check(source)
if no_longer_needed:
    job.cancel()
result = job.result()
```

`cancel()` returns `False` once the job has already completed. Cancellation is
cooperative at the runtime boundary and terminates the local child process.

Native asyncio helpers preserve that cancellation behavior:

```python
result = await environment.check_async(source)
result = await environment.check_files_async(files, entrypoint="Main.lean")
results = await environment.check_many_async(sources, concurrency=8)
```

Mutable project checks and `check_matrix_async()` use the same cancellation signal; cancelling
the coroutine terminates active local Lean processes before control returns to the caller.
Synchronous `setup()`, `check()`, and `Runtime.prepare()` accept a `threading.Event` through
`cancel=`. The signal interrupts toolchain installation, Lake resolution, environment builds,
and waits for another process materializing the same environment. Initial shorthand-reference
discovery and library downloads remain bounded by their transport timeouts.

## Interactive sessions

`spawn_interactive()` keeps a tool alive inside one disposable instance and
exposes line-buffered UTF-8 pipes. This supports NDJSON bridges, REPLs, language
servers, and other processes where startup cost should be paid once:

```python
import json

from lean_runtime import ExecutionPolicy

with environment.spawn_interactive(
    ["lake", "exe", "lean_bridge"],
    policy=ExecutionPolicy(timeout_seconds=3600, memory_mb=4096),
) as session:
    request = {"id": 1, "method": "get_info", "params": {}}
    response = session.request_json(request)
    assert session.running

result = session.close()
assert result.execution_id == session.execution_id
```

`close()` is idempotent. It closes stdin first so cooperative servers can exit
on EOF, waits briefly, then terminates and finally kills the process group if
needed. The disposable instance is removed and the final `ExecutionResult` is
persisted even when `close()` is called by the context manager during exception
unwinding.

`stdout` and `stderr` reads are mirrored into one bounded transcript budget;
the original text is still returned to the caller. Callers of tools that emit
substantial data on both streams should drain both streams to avoid ordinary
subprocess pipe backpressure.

For line-oriented tools, `send_line()` and `read_line()` provide incremental
REPL-style access. `request_line()` serializes one request/response exchange,
and `request_json()` does the same for NDJSON protocols. The request helpers
are locked so concurrent callers cannot interleave protocol frames. The raw
`stdin`, `stdout`, and `stderr` pipes remain available for other protocols.

## Progress events

```python
runtime = Runtime(on_event=lambda event: print(event.kind, event.data))
```

Events describe toolchain readiness, Lake resolution, source locking/cache
hits, artifact hydration, builds, publication, and environment reuse. They are
typed `RuntimeEvent` values rather than parsed log lines.

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
```

Consult `result.provenance.enforced_policy_fields`: requested controls may vary
by backend and platform. The local backend rejects network isolation requests
because it cannot enforce them.

## Result and provenance

`ExecutionResult` includes the exit code, command, output, duration, stable phase timings,
timeout, cancellation and truncation flags, parsed diagnostics, and provenance.
Provenance includes:

- unique `execution_id`;
- stable logical `request_digest`;
- environment and lock identities;
- exact package commits and Git tree hashes;
- toolchain, platform, backend, and policy;
- source digest and start timestamp.

Mutable local-project results have no environment or lock identity. Instead,
their provenance includes the canonical root, a workspace-content digest that
excludes `.git` and `.lake`, Lake configuration and manifest digests, and Git
revision/dirty state when available.

Diagnostic extraction is explicitly best-effort. The original stdout and
stderr remain authoritative.

Before checking, a sparse environment computes the imports' recorded closure,
verifies any missing pack frames into the shared CAS, and writes a versioned
Lean `--setup` file for the projected artifacts. Legacy full environments keep
their direct compiled-module path. Ordinary managed checks do not ask Lake to
rescan or build the dependency graph.
