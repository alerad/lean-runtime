# Python API

Set up one context, then check as many sources as you like. The expensive work
happens on the first call; everything after it reuses the prepared environment.

## Select a context

`setup()` prepares one context and returns a handle:

```python
import lean_runtime as lean

environment = lean.setup(["mathlib@v4.33.0"])
core = lean.setup(toolchain="v4.33.0")

# Alternatives when these caller-provided contexts already exist:
locked = lean.setup(lock="environment.lock.json")
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
source = """\
import Mathlib
example : 2 + 2 = 4 := by norm_num
"""
files = {
    "Helper.lean": "def answer := 42\n",
    "Main.lean": "import Helper\nexample : answer = 42 := rfl\n",
}
sources = [source, "example : True := trivial"]

result = environment.check(source)
result = environment.check_files(files, entrypoint="Main.lean")
results = environment.check_many(sources, concurrency=8)
```

One-shot helpers route the same way:

```python
result = lean.check(source, deps=["mathlib@v4.33.0"])
result = lean.check_file("./my-project/MyProject/Basic.lean")
```

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
runtime = lean.Runtime()
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

## Observe progress

```python
runtime = lean.Runtime(on_event=lambda event: print(event.kind, event.data))
```

Events are typed `RuntimeEvent` values, not parsed log lines. They cover
toolchain readiness, Lake resolution, source locking, artifact restoration,
builds, publication, and environment reuse.

Cancellation, asyncio, replay, and persistent interactive processes are covered
in [Advanced Python workflows](python-advanced.md).
