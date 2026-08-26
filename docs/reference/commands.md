# Commands

This page summarizes the command surface. Run `lean-runtime COMMAND --help` for the installed version's complete arguments and options.

## The model

| Noun | Meaning |
| --- | --- |
| context | Where a file's requirements come from: `--using`, frontmatter, the owning Lake project, or automatic discovery. |
| environment | One exact, immutable toolchain + package set. |
| lock | An environment written down, reusable offline anywhere. |
| verdict | Lean's answer inside one environment: `accepted` or `rejected`. |

Discovery proposes an environment. Only Lean accepts it. `status` shows the
proposal; `check` produces the verdict.

## Daily workflows

| Command | Purpose |
| --- | --- |
| `new NAME` | Create a Lean project. |
| `adopt [PATH]` | Register existing Lake projects and prepare shared dependency storage. |
| `check [PATH ...]` | Check a project or Lean source with a discovered or explicit environment. |
| `watch FILE` | Recheck one project file when it changes. |
| `build [TARGET ...]` | Build the current Lake project. |
| `update [PATH]` | Apply the latest cataloged Mathlib/toolchain update; non-Mathlib projects are a no-op. |
| `publish [PATH]` | Configure project publication. |

## Inspection and maintenance

| Command | Purpose |
| --- | --- |
| `status [SUBJECT]` | Dry run of `check`: the context source, its confidence, the environments discovery would try, and whether a download is needed. `--probe` prices the first download. |
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
| `storage` | Storage usage and verification; see [Storage](storage.md). |
| `program` | Ready-to-run program artifacts. |
| `catalog` | Discovery catalog maintenance. |

## Common check options

```console
lean-runtime check Main.lean
lean-runtime check Main.lean --json
lean-runtime check Main.lean --timings
lean-runtime check Main.lean --repeat 10
lean-runtime check Main.lean --write-lock environment.lock.json
lean-runtime check - --using leanprover/lean4:v4.33.0
```

`-` reads Lean source from standard input and requires an explicit environment.

Automatic discovery is the normal standalone workflow. `--using` selects the
environment explicitly when a package release, lock, toolchain, stored
environment, or project is specifically required. `--offline` prevents
acquisition and therefore needs the selected content to be retained locally.

`--allow-source-build` permits standalone discovery to build an environment from source when no suitable acquired form is available.

## Commands that change things

Commands that change a project, delete local content, or push to a remote
describe the change first and apply it only with `--yes` or an interactive
confirmation. Most also accept `--dry-run`. Non-interactive runs without
`--yes` exit with code `2` and change nothing. See
[Trust](../trust.md#which-commands-change-what) for the full list.

## Machine output

Every command that accepts `--json` writes exactly one JSON envelope to
standard output — never an empty stream — and exits non-zero on failure:

```json
{"schema": "lean-runtime.execution/v1", "ok": true, "data": {...}, "warnings": [], "errors": []}
```

Schemas ship with the package (`lean_runtime.schema_path(NAME)`):
`execution`, `check-batch`, `matrix`, `profile`, `plan`, `status`, `verify`,
`comparison`, `inspect`, `cleanup`, `publication`, and `attestation`, each
suffixed `-v1.schema.json`. An execution result carries `data.verdict`
(`accepted`, `rejected`, or `not_run`) in addition to `data.ok`.

## Build cache control

```console
lean-runtime build
lean-runtime build --no-cache
```

The default build may restore supported dependency artifacts before invoking Lake. `--no-cache` skips that restoration step.

## Exit codes for check

| Code | Verdict | Meaning |
| --- | --- | --- |
| `0` | `accepted` | Lean accepted the source. |
| `1` | `rejected` | Lean ran and rejected the source. |
| `2` | none | Invocation, context, acquisition, or configuration failed, or a resource limit such as `--timeout` was hit. |
| `130` | none | Interrupted. |
