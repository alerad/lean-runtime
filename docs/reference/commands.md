# Commands

This page summarizes the command surface. Run `lean-runtime COMMAND --help` for the installed version's complete arguments and options.

## Daily workflows

| Command | Purpose |
| --- | --- |
| `new NAME` | Create a Lean project. |
| `adopt [PATH]` | Register existing Lake projects and prepare shared dependency storage. |
| `check [PATH ...]` | Check a project or Lean source with inferred or explicit context. |
| `watch FILE` | Recheck one project file when it changes. |
| `build [TARGET ...]` | Build the current Lake project. |
| `update [PATH]` | Plan and apply a project update. |
| `publish [PATH]` | Configure project publication. |

## Inspection and maintenance

| Command | Purpose |
| --- | --- |
| `status [SUBJECT]` | Explain the selected project or standalone context. |
| `verify SUBJECT` | Verify a lock, environment, or artifact. |
| `doctor` | Diagnose the local installation and configuration. |
| `clean` | Preview and reclaim unused runtime storage. |
| `replay CAPTURE` | Replay an execution capture. |
| `completion SHELL` | Generate shell completion. |

## Advanced namespaces

| Namespace | Scope |
| --- | --- |
| `env` | Exact immutable environments and locks. |
| `project` | Mutable project inspection, sharing, locks, and exports. |
| `toolchain` | Toolchain installation, inspection, optimization, and publication. |
| `storage` | Storage usage and verification. |
| `program` | Ready-to-run program artifacts. |
| `catalog` | Discovery catalog maintenance. |

## Common check options

```console
lean-runtime check Main.lean --using CONTEXT
lean-runtime check Main.lean --offline
lean-runtime check Main.lean --json
lean-runtime check Main.lean --timings
lean-runtime check Main.lean --repeat 10
```

`--allow-source-build` permits standalone discovery to build an environment from source when no suitable acquired form is available.

## Build cache control

```console
lean-runtime build
lean-runtime build --no-cache
```

The default build may restore supported dependency artifacts before invoking Lake. `--no-cache` skips that restoration step.

## Exit codes for check

| Code | Meaning |
| --- | --- |
| `0` | Lean accepted the source. |
| `1` | Lean ran and rejected the source. |
| `2` | Invocation, context, acquisition, or configuration failed. |
| `130` | Interrupted. |
