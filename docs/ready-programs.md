# Ready-to-run programs

A **ready-to-run program** is a verified executable result from a Lean project.
It opens quickly because it does not rebuild the project or download Mathlib.

Use a full **environment** when you need the sources, compiler state, kernel
replay, or a new build. Use a ready-to-run **program** when you only need to run
the already-built result.

## Create and run one

```python
from lean_runtime import Runtime

runtime = Runtime()
program = runtime.create_program(
    "build/program",
    command=["my-program"],
    source_revision="0123456789abcdef0123456789abcdef01234567",
    provenance={"example.protocol.version": "1.0.0"},
)

with program.spawn_interactive() as session:
    print(session.request_line("hello"))
```

For an NDJSON checker, `session.request_json({...})` keeps the compiled process
alive and returns one decoded response per request. This is the fast path for
large batches: prepare and verify the checker once, then stream compact inputs
instead of elaborating a new Lean source file for every item.

`create_program` records the program's files, computer compatibility, and
optional flat string provenance. All three contribute to the program ID.
Opening it later with `runtime.program(program.id)` verifies that none has
changed. The CLI accepts the same metadata as a JSON object through
`program create --provenance-file`.

## Move it or share it

Save and open a portable copy:

```bash
lean-runtime program save PROGRAM_ID --output my-program.tar.gz
lean-runtime program open my-program.tar.gz
```

Or use a program library:

```bash
lean-runtime program download ghcr.io/example/lean-programs REVISION
lean-runtime publish program PROGRAM_ID --library ghcr.io/example/lean-programs
```

A library can be public or private. Authentication follows the credentials your
library host already provides. OCI is the underlying transfer format, but it is
not part of the ordinary workflow or vocabulary.

For releases built on several kinds of computers, publish each computer result
and combine them with `finalize program`. Downloading then chooses
the compatible result automatically.
