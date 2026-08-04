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
lean-runtime --cache oci://ghcr.io/owner/cache pull environment.lock.json
lean-runtime export research-stack --output research-stack.oci.tar.gz
lean-runtime --home /tmp/fresh import research-stack.oci.tar.gz --name research-stack
lean-runtime build-and-push environment.lock.json --push-to oci://ghcr.io/owner/cache
lean-runtime check research-stack Main.lean --json
lean-runtime inspect research-stack --packages
lean-runtime env-list
lean-runtime cache-status
lean-runtime doctor
lean-runtime gc
lean-runtime gc --execute
```

`gc` is a dry run unless `--execute` is supplied.

`export` produces a deterministic OCI image-layout archive. `import` verifies
the digest and identity chain, package Git trees, platform compatibility, and a
Lean probe before atomically publishing the environment. See
[Environment bundles](bundles.md) for the format and trust boundary.

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
lean-runtime raw-check ./existing-project/MyProject/Main.lean
lean-runtime project-build ./existing-project MyLibrary
lean-runtime install 4.32.2
```

Without `--with`, the environment-aware `check` command requires an environment
identifier. `raw-check` remains the explicitly unmanaged route. When no
`--project` or `--toolchain` is supplied, it discovers the nearest directory
containing a Lake configuration and `lean-toolchain`, then passes the actual
project-relative file to `lake env lean`.

Add supporting source files with repeatable `--include` options:

```bash
lean-runtime check research-stack Main.lean --include Support/Defs.lean
```

Resolution and materialization print structured lifecycle progress to stderr.
Pass global `--quiet` before the subcommand to suppress it.

Global `--cache` is repeatable and `--prebuilt auto|require|never` controls
transparent cache acquisition. `LEAN_RUNTIME_CACHES` accepts a comma-separated
equivalent and `LEAN_RUNTIME_PREBUILT` sets the default policy.

Use global `--signatures require --trusted-identity ID --trusted-issuer ISSUER`
to require a Cosign-verified publisher. `build-and-push --sign` signs the
published lock-index digest using Cosign's configured keyless or keyed context.
