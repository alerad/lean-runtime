# Verify, understand, compare, and measure

Ordinary checks stay deliberately small:

```bash
lean-run Main.lean
```

The v1 precision tools are opt-in and reuse the same locks, environments, and execution
records as ordinary checks.

## Verify

```bash
lean-runtime verify research-stack
lean-runtime verify environment.lock.json --json
lean-runtime verify research-stack --offline
lean-runtime verify research-stack --rebuild
```

Lock verification parses the canonical lock and recomputes its identity without resolving,
acquiring, or building. Environment verification checks identity, platform compatibility,
embedded Git commits and trees, workspace structure, and a Lean probe. `--offline` refuses
toolchain acquisition. `--rebuild` uses an independent verification build and reports artifact
inventory differences as warnings: source/probe trust and byte equality are distinct claims.

## Explain context and reuse

```bash
lean-run Main.lean --explain
lean-runtime inspect research-stack --explain
```

The first command only explains routing and does not execute Lean. Environment inspection
reports stable decision codes, resolved identity, origin, and platform compatibility.

## Diff

```bash
lean-runtime compare old.lock.json new.lock.json
lean-runtime compare old-environment new-environment --json
```

Diff compares identity inputs rather than directories. Package order is ignored; changed
commits and unchanged trees remain separately visible.

## Timings and profiles

```bash
lean-run Main.lean --timings
lean-runtime --timings check research-stack Main.lean
lean-runtime profile research-stack Main.lean --warmup 1 --repeat 5
```

Warmups are excluded. Every measured sample remains an ordinary persisted execution with a
unique execution ID. Profiling stops at the first rejected or failed sample. Execution JSON
contains the same stable phase records shown by `--timings`. Results list the phases
relevant to that execution; a listed phase that was deliberately skipped is marked
`"performed": false`, and phases that never applied are simply absent.

## Matrix checks

```toml
[[context]]
name = "mathlib-4.31"
requires = ["mathlib@v4.31.0"]

[[context]]
name = "mathlib-4.32"
requires = ["mathlib@v4.32.2"]
```

```bash
lean-runtime matrix matrix.toml Main.lean --concurrency 2
```

Each context uses exactly one of `requires`, `lock`, `environment`, `toolchain`, or
`project`. Preparation and checking use the normal runtime paths, and every entry contains
an ordinary execution result. Concurrency defaults to one and is bounded at 32.
Cancelling the async Python matrix API signals every active Lean process and prevents pending
entries from beginning execution.

## Machine-readable output

The versioned precision surfaces — execution results (`check`, `check-file`,
`lean-run`), multi-file check batches, `verify`, `compare`, `profile`, `matrix`,
`inspect`, `clean`, and `publish environment` — emit a closed envelope:

```json
{
  "schema": "lean-runtime.verify/v1",
  "ok": true,
  "data": {},
  "warnings": [],
  "errors": []
}
```

Schemas are published under `schemas/`. JSON uses stable reason codes; human wording may
improve without a schema change. Other `--json` commands (for example `storage`,
`doctor`, and `environments`) currently emit raw objects and are not yet versioned v1
contracts. Exit code 0 means success, 1 is a completed negative
result, and 2 is invalid invocation or an exceptional runtime failure.

## Reproducible case study

After preparing an exact environment, run:

```bash
python scripts/run_v1_case_study.py research-stack --output results.json
```

The raw output records machine information, identities, warm concurrent checks, bundle
size and timings, import into a fresh store, offline verification, and replay. Cold builds
must be measured separately and reported with their precise cache state.
