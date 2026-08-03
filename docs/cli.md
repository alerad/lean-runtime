# Command-line interface

All commands accept `--home PATH` before the subcommand to select a store.

## Environment workflow

```bash
lean-runtime resolve environment.toml --output environment.lock.json
lean-runtime ensure environment.lock.json --name research-stack
lean-runtime check research-stack Main.lean --json
lean-runtime inspect research-stack --packages
lean-runtime env-list
lean-runtime cache-status
lean-runtime doctor
lean-runtime gc
lean-runtime gc --execute
```

`gc` is a dry run unless `--execute` is supplied.

## Replay

```bash
lean-runtime replay result.execution.json --json
```

Replay ensures the captured lock, reacquires missing exact sources if network
is available, and then runs the captured request. An already published
environment can replay offline.

## Existing projects and core Lean

```bash
lean-runtime raw-check Main.lean --toolchain 4.32.2
lean-runtime raw-check Main.lean --project ./existing-project
lean-runtime project-build ./existing-project MyLibrary
lean-runtime install 4.32.2
```

The environment-aware `check` command requires an environment identifier. The
`--toolchain` option belongs to `raw-check`; this split keeps reproducible
environment execution distinct from ad hoc invocation.

Add supporting source files with repeatable `--include` options:

```bash
lean-runtime check research-stack Main.lean --include Support/Defs.lean
```

Resolution and materialization print structured lifecycle progress to stderr.
Pass global `--quiet` before the subcommand to suppress it.
