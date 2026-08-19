# Trust and limitations

Lean Runtime records exact identities, verifies retained content, and reports the context used for execution. It is not a process sandbox.

## What it establishes

- Exact package revisions and Git tree identities in environment locks
- Toolchain identity
- Content digests for retained and transferred artifacts
- Platform compatibility metadata
- Execution provenance for completed checks
- Compiler acceptance or rejection for the source that ran

## What it does not establish

- That a third-party package is trustworthy
- That a Lake build script is safe to execute
- That package code cannot access the host system
- That integrity verification provides process isolation

Lake packages and build scripts can execute code during acquisition or compilation. Treat unfamiliar dependencies as code you are about to run on your machine.

## Verify retained content

```console
lean-runtime verify environment.lock.json
lean-runtime verify ENVIRONMENT
lean-runtime storage verify
```

Use `--offline` when verification must not contact remote services. Use `verify --rebuild` when independent reconstruction is required and permitted.

## Diagnose the local runtime

```console
lean-runtime doctor
lean-runtime doctor --json
```

`doctor` inspects configuration and local prerequisites. It does not execute a Lean proof as a substitute for `check`.
