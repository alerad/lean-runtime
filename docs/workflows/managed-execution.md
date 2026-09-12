# Managed project execution

`ProjectEnvironment.execute()` launches a batch command in a persistent full
Lake project, using its pinned toolchain environment and recording project and
package provenance. Build outputs remain available to later executions.

```python
from lean_runtime import Runtime, ExecutionPolicy

project = Runtime().project("/work/extraction-workspace")
project.build(targets=("extract",)).raise_for_error()
result = project.execute(
    ["lake", "env", ".lake/build/bin/extract", "--out", "/work/dataset"],
    policy=ExecutionPolicy(timeout_seconds=7200, max_output_bytes=10_000_000),
)
result.raise_for_error()
```

`lean`, `lake`, and `leanc` resolve directly from the pinned toolchain. Other
commands use the project working directory and prepared process environment;
absolute executable paths are accepted. Commands are argument sequences, without
shell interpretation. Use `build()` for coordinated builds; `execute()` allows
independent workers and does not serialize them under the build lock.

Pass `cancel=threading.Event()` for cancellation. `on_bytes(stream, data)` receives
bounded raw stdout/stderr bytes, preserving byte fidelity and order within each
stream. Callbacks are serialized and should return promptly. Callback exceptions
stop the child and propagate. A backend lacking byte capture rejects the request.
Text output and ordinary execution provenance remain in `ExecutionResult`.

Output budgets bound retained/captured bytes; inspect `output_truncated` when
complete output is required. Dataset callers must also validate their artifact
manifest and references before accepting process success. Mutable projects may
lack immutable environment or lock IDs; preserve the actual provenance returned.

This API requires full project execution capabilities. It does not upgrade sparse
checking capsules into source workspaces, resolve corpus dependencies, or provide
a security sandbox. The local backend retains its existing policy capabilities.
