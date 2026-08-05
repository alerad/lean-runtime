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
)

with program.spawn_interactive() as session:
    session.stdin.write("hello\n")
    session.stdin.flush()
    print(session.stdout.readline())
```

`create_program` records the program's files and computer compatibility. Opening
it later with `runtime.program(program.id)` verifies that neither has changed.

## Move it or share it

Save and open a portable copy:

```bash
lean-runtime program-save-copy PROGRAM_ID --output my-program.tar.gz
lean-runtime program-open-copy my-program.tar.gz
```

Or use a program library:

```bash
lean-runtime program-download ghcr.io/example/lean-programs REVISION
lean-runtime program-publish PROGRAM_ID --library ghcr.io/example/lean-programs
```

A library can be public or private. Authentication follows the credentials your
library host already provides. OCI is the underlying transfer format, but it is
not part of the ordinary workflow or vocabulary.

For releases built on several kinds of computers, publish each computer result
and combine them with `program-finalize-publication`. Downloading then chooses
the compatible result automatically.
