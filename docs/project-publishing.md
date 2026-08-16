# Publishing a Lean project

Lean Runtime can freeze a clean Git-backed Lean project into an exact lock,
build it on each supported computer type, and publish verified ready-to-use
environments through an OCI-compatible library. Consumers import the project's
public module without cloning or building the project.

This is distinct from ordinary project CI. [`leanprover/lean-action`](https://github.com/leanprover/lean-action)
builds and tests a checkout; Lean Runtime distributes an immutable environment
to other machines. Projects can use both.

## Local preflight

Inspecting is read-only and performs no build:

```bash
lean-runtime project inspect . --module MyProject
```

Add `--check-remote` to prove that the exact HEAD commit is available from
`origin`. Publication deliberately requires:

- a pinned `lean-toolchain`;
- a root `lakefile.toml` or `lakefile.lean` with an importable Lean library;
- the Lake project at the Git repository root;
- a clean checkout;
- a fetchable `origin` (GitHub, self-hosted HTTPS/SSH, `file://`, or a local
  bare repository); and
- a HEAD commit available from that remote.

If `lakefile.lean` must be translated, its pinned toolchain must already be
installed. Preflight never installs it implicitly.

Freeze the project explicitly when a lock is useful on its own:

```bash
lean-runtime project lock . --module MyProject
```

The default output is `environment.lock.json` in the project root. The lock
contains the exact project commit, complete Lake dependency graph, toolchain,
and selected public module.

GitHub HTTPS and SSH origins are canonicalized to the portable HTTPS form and
receive a `github:OWNER/REPOSITORY@COMMIT` convenience reference. Other origins
are preserved as exact Git sources. Relative local paths become absolute
`file://` URLs, which is useful for offline export but intentionally remains
machine-local unless the resulting capsule is distributed.

## Export this computer

A portable capsule is useful for transferring the current computer's
source-free check environment:

```bash
lean-runtime project export . --module MyProject \
  --output MyProject.lean-environment

lean-runtime copy open MyProject.lean-environment --name MyProject
```

The export contains the selected public module's exact transitive closure, not
the project checkout, Lake metadata, editor indexes, or native/development
outputs. `copy open` verifies every artifact and runs the locked import before
publishing it locally. Capsules are computer-type-specific; use the reusable
workflow for a public multi-platform environment.

## Generate the publication workflow

```bash
lean-runtime project init-publish . \
  --module MyProject \
  --library ghcr.io/OWNER/my-project-environments
```

This creates `.github/workflows/publish-lean-environment.yml`:

```yaml
name: Publish Lean environment

on:
  workflow_dispatch:
  push:
    tags: ["v*"]

permissions:
  contents: read
  packages: write
  id-token: write

jobs:
  publish:
    uses: alerad/lean-runtime/.github/workflows/publish-project.yml@v3
    with:
      project: .
      library: ghcr.io/OWNER/my-project-environments
      module: MyProject
      public: true
    secrets: inherit
```

The maintained workflow validates and locks the checkout once; builds Linux
AMD64, macOS AMD64, and macOS ARM64; differentially verifies a source-free
capsule against the full build; publishes seekable module packs and the matching
slim Lean runtime; signs the two indexes; and makes either index visible only
after every platform succeeds. It then downloads them into clean stores on all
three platforms and checks `import MyProject` without a source-build fallback.

The clean-consumer import has configurable `check-budget-seconds` and
`warm-check-budget-seconds` inputs (300 seconds each by default). These are
both real execution timeouts and regression gates. The first check deliberately
measures a cold filesystem cache immediately after acquisition; the second
measures steady-state use. A project with an intentionally heavier public import
can raise either value explicitly. The workflow reports both selected budgets
and measured check times.

The final acceptance is anonymous when `public: true`. GHCR package visibility
is controlled by GitHub: after the first publication, make the package public
in its package settings. Until then, acceptance intentionally fails rather
than claiming that public consumption works. An incomplete matrix never
replaces the last complete lock index.

## Consumer

The exact lock is the portable consumer contract:

```bash
lean-runtime --library ghcr.io/OWNER/my-project-environments \
  --availability required download environment.lock.json --name MyProject
```

For standalone source, configure the library and use the exact GitHub reference:

```bash
export LEAN_RUNTIME_LIBRARIES=ghcr.io/OWNER/my-project-environments

lean-runtime run Main.lean \
  --with github:OWNER/REPOSITORY@FULL_COMMIT \
  --no-source-build
```

The project must be rooted at the repository root for friendly-reference
discovery. `--no-source-build` makes absence, incompatibility, or registry
visibility a clear failure instead of a local build.

## Performance contract

The release gate tracks phases separately, and publication JSON includes
`total_blob_bytes`, `uploaded_bytes`, `reused_bytes`, and `reuse_percent`:

- local TOML preflight completes in under two seconds and never builds;
- the repeat-publication fixture sets `minimum-reuse-percent: 99`, so CI fails
  unless at least 99% of remote blob bytes are reused;
- registry selection produces visible progress within two seconds;
- verification time is reported separately from download time;
- warm setup remains below 250 ms;
- per-check runtime staging remains below 250 ms;
- first and warm import proofs remain below their explicit consumer budgets; and
- execution scratch space is empty after the check.

Publication reports total, uploaded, and reused bytes. Clean acceptance reports
acquisition seconds, warm setup, and check time separately; acquisition has no
universal wall-clock threshold because it is bandwidth-dominated.
