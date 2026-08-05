# Command-line interface

## `lean-run`

The front-facing command checks one file and discovers its context:

```bash
lean-run Main.lean
lean-run Main.lean --with mathlib@v4.32.2
lean-run Main.lean --lock environment.lock.json
lean-run Main.lean --json
lean-run Main.lean --explain
lean-run Main.lean --timings
```

Use `--lock-out environment.lock.json` with dependencies to retain the exact
resolved graph. See [Standalone Lean files](standalone-files.md) for frontmatter,
routing precedence, conflict rules, and output behavior.

## `lean-runtime`

All commands accept `--home PATH` before the subcommand to select a store.

## One-shot package workflow

```bash
lean-runtime check Main.lean \
  --with github:alerad/leancert@v4.32.2.4
```

`--with` is repeatable. References use
`mathlib@REVISION`, `OWNER/REPOSITORY@REVISION`, or the explicit
`github:OWNER/REPOSITORY@REVISION` form. Package discovery reads the root
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
lean-runtime prepare environment.toml --output environment.lock.json
lean-runtime open environment.lock.json --name research-stack
lean-runtime --library ghcr.io/owner/lean-environments download environment.lock.json
lean-runtime save-copy research-stack --output research-stack.lean-environment
lean-runtime --home /tmp/fresh open-copy research-stack.lean-environment --name research-stack
lean-runtime build-and-publish environment.lock.json --publish-to ghcr.io/owner/lean-environments
lean-runtime check research-stack Main.lean --json
lean-runtime inspect research-stack --packages
lean-runtime environments
lean-runtime storage
lean-runtime doctor
lean-runtime verify research-stack --offline
lean-runtime compare old.lock.json new.lock.json
lean-runtime profile research-stack Main.lean --repeat 5
lean-runtime matrix matrix.toml Main.lean
lean-runtime clean
lean-runtime clean --execute
```

`clean` is a dry run unless `--execute` is supplied.

`save-copy` creates a portable environment file. `open-copy` verifies its exact
identity, package Git trees, computer compatibility, and Lean probe before
making the environment available. See [Portable copies and environment
libraries](portable-copies.md) for its trust boundary.

## Replay

```bash
lean-runtime replay result.execution.json --json
```

Replay ensures the captured lock, reacquires missing exact sources if network
is available, and then runs the captured request. An already published
environment can replay offline.

## Existing projects and core Lean

```bash
lean-runtime check-file Main.lean --toolchain 4.32.2
lean-runtime check-file ./existing-project/MyProject/Main.lean
lean-runtime build ./existing-project MyLibrary
lean-runtime install 4.32.2
```

Without `--with`, the environment-aware `check` command requires an environment
identifier. `check-file` is the direct local-project route. When no
`--project` or `--toolchain` is supplied, it discovers the nearest directory
containing a Lake configuration and `lean-toolchain`, then passes the actual
project-relative file to `lake env lean`.

Add supporting source files with repeatable `--include` options:

```bash
lean-runtime check research-stack Main.lean --include Support/Defs.lean
```

Resolution and materialization print structured lifecycle progress to stderr.
Pass global `--quiet` before the subcommand to suppress it.

Pass global `--timings` before the subcommand for stable phase timing output. Machine-readable
execution output uses the versioned `lean-runtime.execution/v1` envelope; the other v1
schemas and advanced command examples are documented in
[Verify, understand, compare, and measure](v1-precision.md).

Global `--library` is repeatable and `--availability auto|required|local`
controls whether ready-to-use environments are downloaded or built locally.
`LEAN_RUNTIME_LIBRARIES` accepts a comma-separated equivalent and
`LEAN_RUNTIME_AVAILABILITY` sets the default policy.

Use global `--publisher_verification required --trusted-publisher ID --trusted-issuer ISSUER`
to require a verified publisher. `build-and-publish --sign` records the trusted
publisher using the configured Cosign identity.
