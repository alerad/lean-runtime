# Command-line interface

## `lean-runtime run` and `lean-run`

The front door discovers a context and checks one file. `lean-runtime run FILE`
is the canonical main-CLI spelling; `lean-run FILE` is an equivalent
convenience alias with the same behavior, JSON contracts, and exit codes:

```bash
lean-runtime run Main.lean
lean-runtime run Main.lean --with mathlib@v4.32.2
lean-runtime run Main.lean --lock environment.lock.json
lean-runtime run Main.lean --json
lean-runtime run Main.lean --explain
lean-runtime run Main.lean --timings
lean-runtime run Main.lean --lock-out environment.lock.json
lean-runtime run Main.lean --no-source-build
lean-runtime run Main.lean --offline
lean-run Main.lean
```

| Command | Context behavior | Typical use |
|---|---|---|
| `lean-runtime run FILE` | Discovers or selects context | Standalone/front door |
| `lean-run FILE` | Same as `run` | Short convenience spelling |
| `lean-runtime check FILE` | Requires project or explicit context | Project iteration |
| `lean-runtime check` | Checks all local libraries | Project-wide check |
| `lean-runtime build` | Full Lake target build | Executables/native outputs |

Without explicit context or a pinned Lake project, `run` automatically
searches its bundled exact-environment catalog. Use `--lock-out` to retain the
successful lock, `--no-discover` to require explicit context, and
`--catalog PATH` to override the catalog. Three budgets bound the work:
`--search-timeout` covers ranking and compiler probes, `--check-timeout`
covers each Lean invocation, and `--acquire-timeout` independently bounds
downloading, installing, or building one candidate environment, so a slow
first-time download cannot expire the search. See
[Standalone Lean files](standalone-files.md) for routing and policy details.

## `lean-runtime`

All commands accept `--home PATH` before the subcommand to select a store.

For project iteration, `check FILE --watch` rechecks on save with warm native
import snapshots. `check FILE --repeat N` measures the same path (add
`--environment NAME` to profile inside a managed environment), and
`check FILE --across matrix.toml` checks exact contexts.

`storage` reads a fingerprinted ledger after its first inventory, so large
stores remain an instant-information command. `storage --verify` explicitly
rebuilds that ledger and prints a progress line because it must walk the store.
`doctor` is human-readable by default; `doctor --json` preserves automation and
`doctor --fix` applies its safe stale-staging and private-Elan remedies.
`clean --keep-last N` (or `LEAN_RUNTIME_CLEAN_KEEP_LAST`) retains the newest N
otherwise eligible unnamed environments in addition to the age threshold.

## One-shot package workflow

```bash
lean-runtime check Main.lean \
  --with github:alerad/leancert@v4.32.2.4
```

`--with` is repeatable. References use
`mathlib@REVISION`, `OWNER/REPOSITORY@REVISION`, or the explicit
`github:OWNER/REPOSITORY@REVISION` form. Package discovery reads the root
`lean-toolchain` and a root Lake configuration (`lakefile.toml` or
`lakefile.lean`, translated with the package's pinned Lake), pins the
reference to a full commit, and
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
lean-runtime copy save research-stack --output research-stack.lean-environment
lean-runtime --home /tmp/fresh copy open research-stack.lean-environment --name research-stack
lean-runtime publish environment environment.lock.json --publish-to ghcr.io/owner/lean-environments
lean-runtime publish environment --publish-to ghcr.io/owner/lean-environments --check-access
lean-runtime publish environment --publish-to ghcr.io/owner/lean-environments --check-access --json
lean-runtime check research-stack Main.lean --json
lean-runtime inspect research-stack --packages
lean-runtime environments
lean-runtime storage
lean-runtime doctor
lean-runtime verify research-stack --offline
lean-runtime compare old.lock.json new.lock.json
lean-runtime check --environment research-stack Main.lean --repeat 5
lean-runtime check Main.lean --across matrix.toml
lean-runtime clean
lean-runtime clean --execute
```

`clean` is a dry run unless `--execute` is supplied.

`copy save` creates a portable environment file. `copy open` verifies its exact
identity, package Git trees, computer compatibility, and Lean probe before
making the environment available. See [Portable copies and environment
libraries](portable-copies.md) for its trust boundary.

## Slim toolchains

The official Lean toolchain is roughly 2.6 GB installed. Proof checking does
not need all of it: editor indexes, static libraries, the bundled LLVM/clang,
and toolchain sources are only used by editors and native compilation.

```bash
lean-runtime toolchain slim v4.32.2
lean-runtime toolchain slim v4.32.2 --prune-original
```

`toolchain slim` materializes a separate check-profile copy by hardlinking the
kept files (near-zero extra disk), then verifies it against a capability
corpus — elaboration, core tactics, `decide`, `#eval`, metaprogramming, and
`Std` imports — using the slim copy's own `lean`. A copy that fails any probe
is removed and reported.

`--prune-original` uninstalls the full Elan toolchain after verification,
reducing the v4.32.2 footprint from about 2.6 GB to about 2.1 GB. All checking
continues through the slim copy. Lean v4.32 loads every `.olean` facet and
per-module IR during ordinary elaboration, so those artifact classes must
stay; larger reductions require upstream facet-loading changes.

After pruning, source builds of *new* environments and native compilation
need the full toolchain again; reinstall it with `lean-runtime toolchain install`.

Libraries produced by the current publication workflow also carry this
verified check profile directly. A cold consumer downloads that compressed
profile instead of first transferring the full official release. If the
library has no compatible published check runtime, Lean Runtime falls back to
the official Elan installation path. Local `toolchain slim` remains useful for
older stores and for testing a profile before publication.

## Sparse acquisition and capabilities

`lean-runtime run FILE --plan` reads capsule metadata and reports the exact compressed
frames required by `FILE`'s transitive import closure, plus the selected Lean
check-runtime cost. The operation performs no downloads, builds, or publications; constructing
the runtime may still initialize store metadata directories. `--max-download`
applies before acquisition to the combined known cost; an unknown component is
reported explicitly instead of treated as zero.

The ordinary check capability retains `.olean`, `.olean.server`,
`.olean.private`, `.ir`, and `.ir.sig`. This is empirical, not conservative
guesswork: Lean 4.32 and 4.33 reject ordinary imports when the corresponding
server/private or IR facets are omitted. `.ilean` editor indexes are a separate
on-demand capability through `Environment.require_capabilities(["editor"],
imports=[...])`. Native compilation and development builds require a full
environment and full toolchain. Requesting them through
`require_capabilities(["native"])` or `["development"]` is currently
rejected for every managed environment; a full environment provides those
capabilities through its ordinary built workspace instead. Sparse handles do
not yet reject `build()` or arbitrary `execute()` early: against an
incomplete capsule workspace such operations typically fail mid-run, so open
or build the full environment for them.

## Replay

```bash
lean-runtime replay result.execution.json --json
```

Replay ensures the captured lock, reacquires missing exact sources if network
is available, and then runs the captured request. An already published
environment can replay offline.

## Existing projects and core Lean

```bash
lean-runtime check Main.lean --toolchain 4.32.2
lean-runtime check ./existing-project/MyProject/Main.lean
lean-runtime build
lean-runtime init MyProof
lean-runtime init MyProof --core
lean-runtime init --plan --max-download 500MiB
lean-runtime update --plan
lean-runtime project scan ~/research
lean-runtime project attach ~/research --recursive
lean-runtime project attach ~/research --recursive --execute
lean-runtime project detach ./existing-project --execute
lean-runtime toolchain install 4.32.2
```

`check FILE` is the direct standalone/local-project route, and
`check --environment NAME FILE` checks inside a managed environment. When no
`--project` or `--toolchain` is supplied, it discovers the nearest directory
containing a Lake configuration and `lean-toolchain`, then passes the actual
project-relative file to `lake env lean`.

`init` defaults to the newest stable cataloged Mathlib, or adopts an existing
pinned Lake project without changing its graph. `--core` opts out of Mathlib.
It prepares dependencies before atomically publishing a new project. `update`
is the explicit transactional move to the newest cataloged Mathlib; `scan`
records exact existing graphs as future local seeds. `attach` is read-only
unless `--execute` is present;
recursive mode inventories a project tree, reports blockers and estimated disk
recovery, and adopts each valid project transactionally. `detach` is likewise a
preview unless `--execute` is present. Attached projects use shared mode by
default in `lean-runtime build`; detach before requesting `--local`.
The attachment preview separates bytes removed from each checkout, compatible
bytes already present in the shared store, new shared bytes that must be
imported, and estimated machine-level recovery. These quantities differ on a
first attachment because copy-on-write migration can shrink the repository
without immediately freeing the dependency blocks retained by the shared copy.

A new `init` target may be absent, empty, or an otherwise empty Git root, with
an optional existing `AGENTS.md`. Git metadata and the custom guide are
preserved. Planning rejects any other existing contents using the same
validation as execution.
Existing target directories are populated in place after staging verifies, so
`lean-runtime init .` keeps the caller's current working directory valid.
`--name NAME` overrides the inferred Lake package/root module name for a new
project, which is useful when a lowercase repository name needs internal
capitalization such as `IntegralFramework`.

Add supporting source files with repeatable `--include` options:

```bash
lean-runtime check research-stack Main.lean --include Support/Defs.lean
```

Resolution and materialization print structured lifecycle progress to stderr.
One-shot checks show a compact in-place status on interactive terminals while Lean
is working. Redirected output, `--json`, and `--quiet` remain free of human progress;
`--timings` separates command preparation from Lean execution.
Pass global `--quiet` before the subcommand to suppress it.

Pass global `--timings` before the subcommand for stable phase timing output. Machine-readable
execution output uses the versioned `lean-runtime.execution/v1` envelope; the other v1
schemas and advanced command examples are documented in
[Verify, understand, compare, and measure](v1-precision.md).

Global `--library` is repeatable and `--availability auto|required|local`
controls whether ready-to-use environments are downloaded or built locally.
`LEAN_RUNTIME_LIBRARIES` accepts a comma-separated equivalent and
`LEAN_RUNTIME_AVAILABILITY` sets the default policy.

Environment publishing probes repository push access before opening or building
the lock. `--check-access` runs only that content-free preflight and needs no
lock. GHCR publishers can use an authenticated `gh` session; explicit
`LEAN_RUNTIME_REGISTRY_USERNAME` and `LEAN_RUNTIME_REGISTRY_PASSWORD` values take
precedence. Publication exits `3` for authentication/permission denial, `4` for
retryable transport or registry failures, and `5` when remote state is partial
or indeterminate. Success is emitted only after the published manifest is read
back and its digest is verified. See [Portable copies](portable-copies.md#publishing).

## Publish an existing project

```bash
lean-runtime project inspect . --module MyProject --check-remote
lean-runtime project lock . --module MyProject
lean-runtime project export . --module MyProject --output MyProject.lean-environment
lean-runtime project init-publish . --module MyProject \
  --library ghcr.io/owner/my-project-environments
```

`inspect` is read-only. The other commands require a clean root Git project
whose exact HEAD commit is available from its configured origin. GitHub,
self-hosted HTTPS/SSH, and local bare repositories are supported.
`init-publish` creates a small caller for the maintained multi-platform
publication and clean-consumer workflow. See
[Publishing a Lean project](project-publishing.md).

Use global `--publisher-verification required --trusted-publisher ID --trusted-issuer ISSUER`
to require a verified publisher. `publish environment --sign` records the trusted
publisher using the configured Cosign identity.
