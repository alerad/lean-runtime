# Trust and limitations

Lean Runtime records exact identities, verifies retained content, and reports the context used for execution. It is not a process sandbox.

## What it establishes

- Exact package revisions and Git tree identities in environment locks
- Toolchain identity
- Content digests for retained and transferred artifacts
- Platform compatibility metadata
- Execution provenance for completed checks, including `source_digest`, the
  digest of the bytes Lean actually checked, and the environment and lock
  identities it ran in
- Compiler acceptance or rejection for the source that ran, reported as a
  `verdict` attributed to one exact environment

## What it does not establish

- That a third-party package is trustworthy
- That a published environment came from a particular publisher, unless
  `publisher_verification` is set to `required`
- That a Lake build script is safe to execute
- That package code cannot access the host system
- That integrity verification provides process isolation
- That a discovery proposal is compatible before Lean has accepted the file in it

Lake packages and build scripts can execute code during acquisition or compilation. Treat unfamiliar dependencies as code you are about to run on your machine.

## Which commands change what

`check`, `status`, `verify`, `doctor`, and the inspection commands read your
project and may add toolchains, environments, and caches under the runtime home
(`LEAN_RUNTIME_HOME`). They never modify your project directory.

Commands that change a project, delete local content, or push to a remote follow
one rule: they describe the change first and apply it only with `--yes` or an
interactive confirmation.

| Command | Changes |
| --- | --- |
| `new`, `adopt`, `update`, `publish`, `project share`, `project unshare` | Your project directory |
| `clean`, `doctor` repairs, `toolchain optimize --prune-original` | Local content under the runtime home |
| `env publish`, `toolchain publish`, `program publish`, `declaration-index publish` | A remote library |

In a non-interactive session these commands exit with code `2` unless `--yes`
is given, so a script can never mutate or publish by accident.

## Verify retained content

```console
lean-runtime verify environment.lock.json
lean-runtime verify ENVIRONMENT
```

Use `--offline` when verification must not contact remote services. Use `verify --rebuild` when independent reconstruction is required and permitted.

`lean-runtime storage verify` performs a deeper scan of the entire local store.
It is a maintenance operation and can take significant time on a large cache;
see [Storage](reference/storage.md).

## Diagnose the local runtime

```console
lean-runtime doctor
lean-runtime doctor --json
```

`doctor` inspects configuration and local prerequisites. It does not execute a Lean proof as a substitute for `check`.
