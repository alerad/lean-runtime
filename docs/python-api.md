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
```

Exactly one of `deps`, `project`, `lock`, or `environment` is required. The
default `Runtime` is created lazily on the first operation; importing
`lean_runtime` has no filesystem or network side effects. Supply
`runtime=Runtime(...)` to any façade function for explicit configuration.

One-shot helpers use the same routing:

```python
result = lean.check(source, deps=["mathlib@v4.32.2"])
result = lean.check_file("./my-project/MyProject/Main.lean")
result = lean.replay("execution.capture.json")
```

## Results

Results remain inspectable values. Scripts that prefer exceptions can use:

```python
result.raise_for_error()
```

Rejection raises `LeanCheckError`, whose `result` attribute retains diagnostics,
stdout, stderr, environment and execution identities, and complete provenance.

## Explicit Runtime API

`Runtime` exposes resolution, stores, policies, publishing, and lifecycle
operations directly:

```python
runtime.resolve(spec)  # -> EnvironmentLock
runtime.ensure(lock, name="friendly-name")  # -> Environment
runtime.open("friendly-name")  # -> Environment, offline
runtime.check(source, environment=spec)  # convenience path
runtime.check(source, packages=["github:owner/repository@v1.0.0"])
runtime.replay_capture("run.json")  # replay a capture
runtime.gc(dry_run=True)  # inspect reclaimable environments
```

Configure transparent prebuilt environments and publish them through OCI:

```python
runtime = Runtime(
    caches=["oci://ghcr.io/alerad/leancert-runtime"],
    prebuilt="auto",
)
environment = runtime.ensure(lock)
published = runtime.publish_environment(
    environment.id,
    "oci://ghcr.io/alerad/leancert-runtime",
    tags=["v4.32.2.4"],
)
```

Package-reference discovery is also exposed in separable stages:

```python
spec = runtime.spec_from_references(["github:alerad/leancert@v4.32.2.4"])
lock = runtime.resolve_references(["github:alerad/leancert@v4.32.2.4"])
environment = runtime.ensure_references(
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
    session.stdin.write(json.dumps(request) + "\n")
    session.stdin.flush()
    response = json.loads(session.stdout.readline())
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

`ExecutionResult` includes the exit code, command, output, duration, timeout,
cancellation and truncation flags, parsed diagnostics, and provenance.
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

Before checking, the environment asks Lake to build imported roots matching
locked packages. This makes transitive package modules available on demand even
when the synthetic environment root did not originally require them.
