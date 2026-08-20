# Python API

Set up one context, then check as many sources as you like. The expensive work
happens on the first call; everything after it reuses the prepared environment.

## Select a context

`setup()` prepares one context and returns a handle:

```python
import lean_runtime as lean

environment = lean.setup(["mathlib@v4.33.0"])
locked = lean.setup(lock="environment.lock.json")
existing = lean.setup(environment="research-stack")
core = lean.setup(toolchain="v4.33.0")
project = lean.setup(project="./my-project")
```

Exactly one of `deps`, `project`, `lock`, `environment`, or a bare `toolchain`
is required. The same precedence applies here as on the command line; see
[Context selection](../concepts/context-selection.md).

Importing `lean_runtime` has no filesystem or network side effects. The default
`Runtime` is created on the first operation. Pass `runtime=Runtime(...)` to any
function for explicit configuration.

## Check a source

```python
result = environment.check(source)
result = environment.check_files(files, entrypoint="Main.lean")
results = environment.check_many(sources, concurrency=8)
```

One-shot helpers route the same way:

```python
result = lean.check(source, deps=["mathlib@v4.33.0"])
result = lean.check_file("./my-project/MyProject/Main.lean")
result = lean.replay("execution.capture.json")
```

`replay()` requires an existing execution capture; the filename above is a
placeholder for an artifact supplied by the caller or an execution backend.

## Read the result

Failed checks expose parsed diagnostics directly:

```python
for error in result.errors:
    print(error.file, error.line, error.message)

if result.first_error is not None:
    print(result.first_error.location)
```

`errors` and `warnings` are severity-filtered views of `diagnostics`. Diagnostic
extraction is best-effort; `stdout` and `stderr` remain authoritative.

Scripts that prefer exceptions can use `result.raise_for_error()`, which raises
`LeanCheckError` on rejection. Its `result` attribute retains diagnostics,
output, environment and execution identities, and provenance.

## Discover an environment

`lean-runtime check FILE` discovers a context automatically for standalone
files. The same bounded, compiler-authoritative search is available directly:

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

Every successful result carries the exact `EnvironmentLock` Lean accepted. Pass
that lock to ordinary operations to skip discovery on later runs.

`outcome` and `completion` are reported separately, so a search stopped by a
candidate, acquisition, or time limit reads as inconclusive rather than as an
exhaustive rejection:

```python
if result.outcome == "inconclusive":
    print(result.completion)  # candidate_limit | acquisition_limit | time_limit
```

Planning metadata narrows candidates. It never asserts compatibility.

## Bound an execution

```python
from lean_runtime import ExecutionPolicy

policy = ExecutionPolicy(
    timeout_seconds=30,
    max_output_bytes=1_000_000,
    cpu_seconds=20,
)
result = environment.check(source, policy=policy)
```

Consult `result.provenance.enforced_policy_fields`: requested controls vary by
backend and platform. The local backend rejects network isolation because it
cannot enforce it. Memory limits are also backend-dependent. In particular,
the local Linux backend currently applies `memory_mb` as a virtual-address-space
limit, so Lean may need substantially more headroom than its resident memory
suggests. Add that limit only after measuring the selected toolchain and backend.

## Provenance

`ExecutionResult` records the exit code, command, output, duration, phase
timings, timeout/cancellation/truncation flags, diagnostics, and provenance.
Provenance includes the `execution_id`, a stable `request_digest`, environment
and lock identities, exact package commits and Git tree hashes, toolchain,
platform, backend, policy, source digest, and start time.

Mutable project results have no environment or lock identity. Their provenance
instead records the canonical root, a workspace digest excluding `.git` and
`.lake`, Lake configuration and manifest digests, and Git revision state.

## Work with Lake projects

```python
project = runtime.project("./existing-project")
project.check_file("./existing-project/MyLibrary/Main.lean")
project.build(("MyLibrary",))
```

`build()` may restore dependency artifacts before invoking Lake. Opt out when
measuring, or when a source-only build is required:

```python
project.build(("MyLibrary",), artifact_cache=False)
```

`runtime.project()` also accepts a contained file and resolves the nearest
pinned Lake root. See [Work with Lake projects](../workflows/lake-projects.md).

## Cancel work in flight

```python
job = environment.start_check(source)
if no_longer_needed:
    job.cancel()
result = job.result()
```

`cancel()` returns `False` once the job has completed. Cancellation is
cooperative and terminates the local child process.

The asyncio helpers preserve that behaviour:

```python
result = await environment.check_async(source)
results = await environment.check_many_async(sources, concurrency=8)
```

Synchronous `setup()`, `check()`, and `Runtime.prepare()` accept a
`threading.Event` through `cancel=`, which interrupts toolchain installation,
Lake resolution, and environment builds.

## Keep a tool alive

`spawn_interactive()` holds one process open inside a disposable instance and
exposes line-buffered UTF-8 pipes, for REPLs, language servers, and NDJSON
bridges where startup cost should be paid once:

```python
with environment.spawn_interactive(["lake", "exe", "lean_bridge"]) as session:
    response = session.request_json({"id": 1, "method": "get_info", "params": {}})

result = session.close()
```

`close()` is idempotent, closes stdin first so cooperative servers exit on EOF,
then terminates the process group if needed. `send_line()`, `read_line()`,
`request_line()`, and `request_json()` are locked so concurrent callers cannot
interleave protocol frames. `lean_bridge` is an example project executable, not
a program bundled with Lean Runtime; replace it with a line-oriented tool provided
by the selected environment.

## Observe progress

```python
runtime = Runtime(on_event=lambda event: print(event.kind, event.data))
```

Events are typed `RuntimeEvent` values, not parsed log lines. They cover
toolchain readiness, Lake resolution, source locking, artifact restoration,
builds, publication, and environment reuse.
