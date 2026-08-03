# Python API

## Runtime

`Runtime` is the main entry point:

```python
runtime.resolve(spec)  # -> EnvironmentLock
runtime.ensure(lock, name="friendly-name")  # -> Environment
runtime.open("friendly-name")  # -> Environment, offline
runtime.check(source, environment=spec)  # convenience path
runtime.replay_capture("run.json")  # replay a capture
runtime.gc(dry_run=True)  # inspect reclaimable environments
```

Raw helpers remain available for core-only snippets and existing Lake projects:

```python
runtime.check(source, toolchain="4.32.2")
runtime.check(source, project="./existing-project")
runtime.build("./existing-project", targets=("MyLibrary",))
```

## Environment

```python
result = environment.check(source)
results = environment.check_many(sources, concurrency=8)
build = environment.build(("RuntimeEnvironment",))
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

Diagnostic extraction is explicitly best-effort. The original stdout and
stderr remain authoritative.
