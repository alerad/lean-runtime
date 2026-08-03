# Command-line interface

All commands accept `--home PATH` before the subcommand to select a store.

## One-shot package workflow

```bash
lean-runtime check Main.lean \
  --with github:alerad/leancert@v4.32.2.4
```

`--with` is repeatable. References use
`github:OWNER/REPOSITORY@TAG-OR-COMMIT`. Package discovery reads the root
`lean-toolchain` and `lakefile.toml`, pins the reference to a full commit, and
then uses the normal lock and environment pipeline. Multiple discovered
packages must declare the same toolchain unless `--toolchain` explicitly
selects the compatibility build.

Supporting files work here too:

```bash
lean-runtime check Main.lean \
  --with github:alerad/leancert@v4.32.2.4 \
  --include Support/Defs.lean
```

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

Without `--with`, the environment-aware `check` command requires an environment
identifier. `raw-check` remains the explicitly unmanaged route.

Add supporting source files with repeatable `--include` options:

```bash
lean-runtime check research-stack Main.lean --include Support/Defs.lean
```

Resolution and materialization print structured lifecycle progress to stderr.
Pass global `--quiet` before the subcommand to suppress it.
