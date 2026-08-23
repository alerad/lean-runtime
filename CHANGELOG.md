# Changelog

## Unreleased

## 4.21.0 - 2026-08-23

## 4.2.0 - 2026-08-23

- Add a scheduled catalog-update workflow that detects a new stable Mathlib
  release, freezes its exact lock, rebuilds the bundled catalog, and opens a
  pull request.
- Add `catalog build --previous`, which reuses module inventories from a
  prior catalog for entries whose locks are unchanged, so incremental
  rebuilds only materialize new or changed sources.
- Add digest-pinned, per-package declaration-index shards generated from
  public `.ilean` files during catalog publication. Rejected standalone
  checks can lazily fetch only the relevant shard and report the module that
  defines an unknown name; retained shards remain available offline and are
  reused across environments with the same source and toolchain.

## 4.1.1 - 2026-08-21

- Escape source stems that are not plain Lean identifiers when rendering
  check setups, so files such as `with space.lean` check correctly inside
  acquired environments.
- Report a hit `--timeout` as `timed out` with exit 2 instead of presenting
  it as a Lean rejection with exit 1, and document the classification.
- Say `--using` in the frontmatter conflict message instead of the removed
  `--with` spelling.
- Re-measure the README and landing-page example timings against this
  machine's warm store and narrow the README hero import to
  `Mathlib.Tactic.NormNum`.
- Accept the `lean:vX.Y.Z` shorthand under the explicit `toolchain:` context
  prefix instead of passing the raw string to Elan.
- Exit 2 when a guided mutation (`new`, `adopt`, `update`, `publish`,
  `project unshare`) needs confirmation but cannot prompt; interactive
  declines still exit 0.
- Rewrite staged scratch paths to the submitted file names in multi-file
  check diagnostics, matching single-file output.
- Render standalone `status` imports, candidates, and availability as
  aligned text instead of Python reprs.
- Hint at `lean-runtime check -` when a project-wide check finds no pinned
  project and stdin is redirected, and document the stdin spelling.
- Document that a project context applies to files inside the project root,
  and list `replay` and `completion` in the README command summary.

## 4.1.0 - 2026-08-19

## 4.0.1 - 2026-08-18

- Keep `verify ENV --offline` strictly local for sparse environments by probing
  a retained capsule module and forbidding projection acquisition during the
  probe.
- Pin the selected exact toolchain before project scaffolding and put a reused
  user toolchain ahead of private Elan for nested Lake commands, preventing
  `new --core` from downloading an alternate unprefixed toolchain.

## 4.0.0 - 2026-08-18

- Replace the implementation-shaped CLI with a cwd-first workflow: `new`,
  `adopt`, universal `check`, `watch`, `build`, `update`, `publish`, `status`,
  `verify`, `doctor`, and `clean`.
- Remove the `lean-run` and `lean-runtime-catalog` entry points and all v3
  compatibility spellings. Exact environment operations now live under `env`;
  advanced project operations use `project info/scan/share/unshare/lock/export`.
- Merge standalone discovery into `lean-runtime check`. A single `--using`
  override replaces the old project, package, lock, environment, and toolchain
  context flags.
- Make cwd the implicit subject for project commands. Adoption detects whether
  its subject is one Lake project or a tree, previews the storage transition,
  confirms interactively, and preserves atomic rollback.
- Detect compatible user Elan toolchains automatically and use them read-only.
  Missing toolchains are still installed in Lean Runtime's private Elan home,
  and optimization never prunes a user-managed installation.
- Make cleanup, adoption, updates, publication setup, and safe doctor repairs
  guided operations. Automation uses `--yes`; inspection uses `--dry-run`.

## 3.0.3 - 2026-08-18

- Give sparse-environment and slim-toolchain downloads independent progress
  lifecycles, so the toolchain transfer reports its real size instead of
  inheriting a completed environment total.
- Reject duplicate members and symlink extraction destinations in
  slim-toolchain archives, matching the portable-environment boundary.
- Correct the announcement gate's versioned plan-envelope parsing and add the
  missing macOS Intel clean-consumer job.
- Expand release acceptance coverage for archive limits and traversal,
  deterministic multi-platform finalization, duplicate platforms, supported
  Cosign releases, missing Cosign, and publisher identity/issuer mismatches.

## 3.0.2 - 2026-08-17

- Route the first check directly through a newly downloaded slim Lean runtime.
  Command construction and executable hashing now re-evaluate toolchain state
  after remote acquisition and ignore incomplete Elan toolchain directories,
  preventing cold discovery from starting an unintended full toolchain install.


## 3.0.1 - 2026-08-17

- Point the portable-copy consuming examples at
  `ghcr.io/alerad/lean-runtime-cache`, which serves check capsules a 3.0 client
  can read, and make the publishing examples use the `ghcr.io/OWNER/...`
  placeholder style the other pages use. The previous
  `ghcr.io/alerad/leancert-runtime` examples named a library that now holds only
  pre-capsule content.
- Make standalone discovery acquire the verified slim Lean runtime inside the
  acquisition budget, leaving the candidate timeout exclusively for the Lean
  compiler probe and preserving the specific timeout diagnostic.
- Make `run --offline` genuinely retained-only: a missing exact environment
  now fails before source materialization, Elan installation, or network
  access.
- Make every `run --explain --json` routing shape validate against the closed
  `lean-runtime.inspect/v1` schema.
- Return exit 130 with a concise message when either `lean-run` or
  `lean-runtime run` is interrupted during preparation.


## 3.0.0 - 2026-08-16

The first release with one canonical command vocabulary, capsule-only
environment libraries, and documentation that states guarantees the
implementation actually keeps.

### Removed (breaking)

- Removed the hidden hyphenated compatibility spellings. Every accepted command
  is now public and appears in help and shell completion:

  | Removed | Canonical replacement |
  |---|---|
  | `save-copy` / `open-copy` | `copy save` / `copy open` |
  | `build-and-publish` / `finalize-publication` | `publish environment` / `finalize environment` |
  | `toolchain-publish` / `toolchain-finalize-publication` | `publish toolchain` / `finalize toolchain` |
  | `toolchain-slim` / `install` | `toolchain slim` / `toolchain install` |
  | `program-create` / `program-save-copy` / `program-open-copy` / `program-download` | `program create` / `program save` / `program open` / `program download` |
  | `program-publish` / `program-finalize-publication` | `publish program` / `finalize program` |
  | `check-file FILE` | `check FILE` (or `run FILE` for standalone discovery) |
  | `profile ENV FILE` | `check --environment ENV FILE --repeat N` |
  | `matrix MATRIX FILE` | `check FILE --across MATRIX` |
  | top-level `scan` / `attach` / `detach` | `project scan` / `project attach` / `project detach` |

- Removed the legacy positional `check ENVIRONMENT FILE` form; use
  `check --environment NAME FILE`.
- Removed the deprecated `run` aliases `--discovery-timeout` and `--timeout`;
  use `--search-timeout` and `--check-timeout`. Removed the undeclared
  `init --mathlib` alias in favor of `--mathlib-version`.
- Environment libraries are capsule-only. Removed the legacy full-bundle
  registry paths: `OCIEnvironmentCache.pull`, `OCIEnvironmentCache.plan`, the
  full publication profile, and the `DownloadUnavailable` fallbacks in
  `Runtime.open_exact` and `Runtime.plan_exact`. Publication always writes the
  `capsule-lock_<sha>` canonical reference, and an environment library object
  must implement `pull_capsule`/`plan_capsule`. Complete source-bearing
  environments are unaffected: source builds, `copy save`, and `copy open` all
  keep working, and `copy open` still reads both archive formats.

### Changed (breaking)

- Program provenance renames `exact_environment_id` to `source_lock_id`
  (`ProgramDescription`, `Runtime.create_program`, and
  `program create --source-lock-id`), because the field always held a lock
  identity. Program copies written with the old key still load.

### Added

- `lean-runtime run FILE` is the canonical front-door spelling. `lean-run`
  remains a permanently supported alias sharing one parser and implementation,
  with identical results, envelopes, and exit codes. Global `--home`,
  `--quiet`, `--verbose`, and `--timings` are accepted both before and after
  `run`, and global `--library`/`--availability` reach the front door with
  explicit conflict errors against `--offline` and `--no-source-build`.
- A misplaced `lean-runtime FILE.lean` invocation suggests `lean-runtime run`.
- `lean-runtime.plan/v1` ships as an installed schema, and the `check-batch`
  and `attestation` schemas are registered with the schema resources API.

### Fixed (guarantees)

- `availability="local"` now forbids extending a sparse projection. Missing
  import closures fail with an actionable error naming the roots instead of
  silently contacting a configured library.
- Every path that shells out to Lake installs the full toolchain first
  (package-graph resolution and materialization, local project probing, and
  managed build/lake execution), so the documented source fallback no longer
  breaks after a slim check toolchain has been acquired. Package-free core
  environments keep their check-only fast path.
- Capability handling is representation-aware: `native`/`development` requests
  are a no-op on a full environment and rejected only on a check capsule, and
  `build()`/`execute()` fail fast on a capsule instead of failing mid-run
  against incomplete inputs. `Environment.sparse` exposes the representation.
- Sparse acquisition holds a CAS collection lease across unpacking and
  projection, so a concurrent `clean --include-downloads` cannot reclaim an
  artifact that is being projected.
- `--attest` publishes a versioned `lean-runtime.attestation/v1` predicate
  carrying the verification report plus a stable `build_inventory` of the Lake
  build outputs, described by `schemas/attestation-v1.schema.json`.
- Sparse downloads record the informational platform record in
  `metadata["platform"]` like every other representation.

### Changed

- `check --repeat` accepts `--environment NAME`, covering everything the
  removed `profile` command did.
- Internal dispatch uses canonical command identities (`publish-environment`,
  `copy-save`, `program-create`, `toolchain-slim`, …) instead of rewriting
  canonical invocations back onto legacy spellings.
- Shell completion and top-level help derive from one public command list; the
  private argparse help-hiding hack is gone. Command help disambiguates the
  vocabulary across `run`, `check`, `open`, `prepare`, `download`, and `build`.
- Bundled GitHub workflows and the `publish-environment` composite action use
  the canonical publication commands.
- Documentation matches the implementation: corrected
  `--publisher-verification` spelling, documented `capsule-lock_<sha>`
  publication references, named the actual verification check codes, scoped the
  credential-retention claim, noted `lakefile.lean` package-reference support,
  scoped the closed JSON-envelope claim to the versioned precision surfaces, and
  replaced "side-effect-free" planning claims with the precise guarantee.

## 2.10.0 - 2026-08-16

- Make header snapshots opt-in (`LEAN_RUNTIME_HEADER_SNAPSHOTS=1`; `--watch` and
  `--repeat` enable them automatically), key them by module identity, load
  existing snapshots without holding the creation lock, cancel lock waits
  promptly, and retry once without a snapshot—quarantining it—when a snapshot
  check times out or reports snapshot errors.
- Support `check FILE...` and `check DIRECTORY...` with independent per-file
  results, `--concurrency`, a `lean-runtime.check-batch/v1` JSON envelope, and
  an explicit `--environment NAME`; the legacy positional `ENVIRONMENT FILE`
  form now applies only when the first argument is not an existing path.
- Attribute shared workspace lock waits to their holder (PID and operation),
  announce header snapshot waits, and record `workspace_lock` and
  `header_snapshot` phase timings so coordination cost is visible next to the
  actual Lean check time.

## 2.9.2 - 2026-08-16

- Keep one fail-closed publication authentication session from push preflight through
  upload, propagate explicit publication timeouts to credential and registry I/O,
  and report credential-provider failures without labeling them anonymous.
- Use logical project names during atomic initialization, show compact TTY-only check
  progress, cache Lean executable identities across invocations, and report check
  command-preparation timing.
- Track disposable execution and resolution workspaces with process-held leases;
  include their footprint in storage reporting and reclaim abandoned workspaces with
  `clean` while protecting active work.

## 2.9.1 - 2026-08-15

- Make GHCR credential discovery a single bounded GitHub CLI status read so a
  transient identity lookup cannot silently downgrade an authenticated account
  to anonymous; add a validated `lean-runtime.publication/v1` envelope behind
  `publish environment --json`.

## 2.9.0 - 2026-08-15

- Make environment publication fail closed: verify registry push access before
  building, report the selected credential source, remotely verify every
  manifest, emit an explicit terminal failure event, and reserve exit codes 3,
  4, and 5 for permission, retryable transport, and partial/indeterminate
  publication failures respectively.
- Make standalone `check FILE` failures point directly to `--toolchain`, and
  have project checks ask Lake to materialize a missing local import before one
  automatic retry.
- Accept any fetchable Git origin for immutable project locks and local exports,
  including self-hosted and local bare repositories; GitHub origins retain
  their canonical `github:` convenience reference.

## 2.8.0 - 2026-08-14

- Cut repeated project-check latency with capability-probed Lean header snapshots;
  `check --watch` rechecks on save, while `check --repeat` and `check --across`
  absorb the common profile/matrix workflows without removing legacy aliases.
- Suggest exact-workspace declarations after coded unknown-identifier diagnostics
  using persisted `.ilean` indexes from the pinned dependency graph.
- Make `init --mathlib-version VERSION` unambiguous, preserve common repository
  scaffolding, optionally generate matching CI with `--ci`, and retain the old
  `--mathlib` spelling as a validated compatibility alias.
- Add `project scan/attach/detach/update`, `publish KIND`, `finalize KIND`, and
  `copy save/open` namespaces while retaining existing command spellings for
  automation.
- Give environments and doctor human output plus `--json`, add `doctor --fix`,
  fingerprinted storage accounting with `storage --verify`, cleanup nudges,
  `clean --keep-last` retention, curated help, and generated Bash/Zsh/Fish
  completions.
- Report cumulative frame and byte counters while restoring sparse capsules, so
  both terse and verbose cold-start output shows measurable forward progress.

## 2.7.0 - 2026-08-14

- Keep newly initialized project roots narrow: the generated root imports its
  local `Basic` module without redundantly importing the all-Mathlib umbrella,
  and the completion hint points to the fast first-file check.
- Render stdin diagnostics as `<stdin>` in the CLI instead of exposing the
  disposable `.lake/lean-runtime` staging path.
- Add fileless `lean-runtime check` and `ProjectEnvironment.check_all()` to
  check every declared local library through Lake's `leanArts` facets, retaining
  correct intra-project dependency ordering without building executables.
- Integrate an ABI- and toolchain-isolated root-project Lake artifact cache for
  projects created by `init`. Capability support is probed and persisted rather
  than version-gated; dependencies remain in the verified shared workspace.
- Keep remote Lake mappings fail-closed until artifacts have a verified SHA-256
  inventory and lazy restoration participates in acquisition planning and
  download limits.

## 2.6.2 - 2026-08-14

- Keep an existing initialization target's directory inode alive, so running
  `lean-runtime init .` no longer strands the invoking shell in an unlinked
  working directory. Fully prepared staged children are published with rollback
  while Git metadata, custom agent guidance, and index state remain in place.
- Make attachment plans distinguish checkout bytes removed, compatible shared
  bytes already available, new shared bytes required, and estimated machine
  recovery. Exact managed packages now bypass redundant source resolution when
  their toolchain, platform, revision, and effective dependency closure match.
- Emit package-by-package shared-workspace resolution, reuse, and import events,
  and add `lean-runtime --version`.

## 2.6.1 - 2026-08-14

- Allow `lean-runtime init .` at an otherwise empty Git repository root while
  preserving its original `.git` directory or worktree file, HEAD, and index.
  Planning and execution now share target validation, so `init --plan` rejects
  unsupported existing contents before reporting an acquisition-ready plan.
- Add `init --name NAME` for repository directories whose filesystem spelling
  should not determine the Lake package and Lean root module name. Repeating
  the same named initialization is idempotent; a conflicting name is rejected.
- Make `init --plan --offline` and `update --plan --offline` genuinely
  network-free. Plans now report explicit blockers and return a failing status
  when an exact local graph or required full toolchain is unavailable.

## 2.6.0 - 2026-08-14

- Make the local-project path a four-command workflow: `init`, `check`,
  `build`, and `update`. New projects use the newest stable cataloged Mathlib
  by default; `--core` opts out, while `--mathlib VERSION` selects a release.
- Make `init` adopt an existing pinned Lake project without changing its graph,
  or create a new project transactionally after its exact dependencies are
  ready. `--plan`, `--offline`, `--max-download`, and `--seed-from` expose its
  acquisition policy, and failed initialization leaves no partial project.
- Add persistent discovery of exact local Lake graphs. `scan` registers existing
  projects as zero-download seeds for future initialization and updates.
- Preserve verified sparse-capsule build artifacts when materializing exact Git
  sources for a mutable project, instead of throwing them away and forcing a
  dependency rebuild.
- Add explicit, transactional `update` to the newest cataloged Mathlib, with an
  old/new revision and toolchain preview and rollback after failed adoption.
- Include exact package revisions, Git tree hashes, and the shared workspace ID
  in mutable-project check and build provenance.

## 2.5.0 - 2026-08-14

- Make `lean-runtime init` create a concise `AGENTS.md` with build, checking,
  and shared-dependency safety instructions by default. `--no-agents` opts out,
  and existing guides are preserved.
- Make `scripts/release.sh` provision an isolated release environment and allow
  safe retries after a pre-tag failure instead of depending on undeclared
  packages in the invoking Python environment.

- Add shared Lake project onboarding and migration. `lean-runtime init` creates
  a standard core or cataloged-Mathlib project; `attach` previews or
  transactionally adopts one project or an entire recursive tree; and `detach`
  materializes independent copies again. Adoption preserves root build output,
  rejects dirty or mismatched dependencies, verifies both generated overrides
  and ordinary Lake package links, and rolls back failed swaps. Attached
  projects automatically use shared mode in `lean-runtime build`.
- Add content-addressed Lake dependency workspaces. Each exact package is reused
  across root manifests when its effective transitive closure, toolchain, and
  platform match. Concurrent runtime builds are serialized, existing clean
  artifacts seed the store with copy-on-write clones, and local Git object
  databases seed other revisions without another network clone. Plain
  `build --shared` never modifies the checkout; only explicit
  `attach --execute` removes verified generated duplicates.

- Add versioned check capsules: publisher-built manifests record every retained
  module facet, exact import edges, content digest, package owner, and
  capability. Publication now fails unless the full environment and a
  physically isolated capsule both accept the locked public import.
- Replace monolithic environment pulls with deterministic seekable zstd packs.
  A clean check downloads only the pack frames covering its transitive import
  closure; verified raw artifacts are shared in a cross-environment CAS and
  projected atomically. `lean-run --plan` reports closure and check-runtime
  costs before transfer, and `--max-download` gates their combined known cost.
- Publish independently verified check-only Lean toolchains through the same
  multi-platform OCI library. Consumers prefer the slim signed runtime before
  installing the full official toolchain, while retaining an Elan fallback for
  libraries that have not published the new artifact yet.
- Add explicit sparse capabilities. Batch checking includes every olean facet
  and IR file Lean actually requires; editor indexes are acquired only when
  requested. Native/development requests fail with an actionable full-build
  requirement instead of pretending that a check capsule supports them.
- Make project export source-free and closure-scoped. `project export` now
  creates a portable sparse capsule, and `open-copy` verifies its OCI digest
  chain, per-artifact CAS hashes, exact lock/platform identity, and Lean probe
  before publishing it locally.
- Include shared sparse artifacts in `lean-runtime storage` and safely reclaim
  old unleased CAS entries with `clean --include-downloads`.
- Make releases tag-driven and fail closed: the helper waits for CI on the exact
  release commit; the tag workflow independently checks version, changelog,
  tests, docs, wheel metadata, and wheel installation; PyPI must succeed before
  the GitHub release is created.
- Extend the bundled catalog through Mathlib v4.33.0 and add exact LeanCert
  environments for the supported Lean 4.30, 4.31, 4.32, and 4.33 lines.
  Otherwise-equivalent discovery candidates now prefer the smallest exact
  dependency closure, so LeanCert support does not enlarge ordinary Mathlib
  checks.
- Make the public-cache post-publication consumer genuinely anonymous by
  withholding registry credentials during its clean download and import probe.
- Verify imported immutable environments through their compiled module paths,
  avoiding Lake dependency resolution that could attempt network clones after
  a successful offline OCI import.
- Add a strict existing-project publication journey: `lean-runtime project
  inspect`, `project lock`, `project export`, and `project init-publish` turn a
  clean, pushed root GitHub Lean project into an exact managed environment.
- Add a public reusable GitHub workflow that builds Linux AMD64, macOS AMD64,
  and macOS ARM64 environments, signs and attests their artifacts, finalizes
  the OCI index atomically, and runs clean anonymous import checks on every
  platform.
- Validate registry configuration inside the CLI error boundary so malformed
  libraries produce concise invocation errors instead of Python tracebacks.
- Report total, uploaded, and remotely reused OCI blob bytes (plus the reuse
  percentage) in every environment publication result. Reusable workflows can
  set `minimum-reuse-percent` to make regressions fail the publication job.
- Emit visible progress for bundle export, blob upload/reuse, platform
  manifests, attestations, and final index signing during long publications.
- Publish from the verified OCI layout directly, avoiding the redundant outer
  gzip archive and immediate re-extraction previously performed before upload.
- Compress deterministic OCI layers at gzip level 6 instead of Python's
  level-9 default; a representative Mathlib subtree encoded about eight times
  faster for roughly two percent more bytes.
- Exclude clone-specific Git administration data from package layers and carry
  a verified deterministic source-tree inventory instead. Identical dependency
  trees now produce identical blobs across clean builders, so OCI reuse works
  across project revisions rather than only within one local environment.
- Omit Lake's path-sensitive `.trace`, `.setup.json`, and response-file metadata from
  check-profile layers. Lean checks use the retained compiled artifacts, while
  clean builders no longer invalidate multi-gigabyte blobs solely because their
  temporary workspace paths differ.

## 2.2.0 - 2026-08-12

- Make published-environment checks invoke Lean directly with the immutable
  compiled module roots instead of cloning the multi-gigabyte workspace and
  asking Lake to rescan its full build graph on every proof. Multi-file checks
  compile support modules into the disposable scratch tree; build and execute
  operations keep their existing isolated workspace clones.
- Resolve exact bundled references such as `mathlib@v4.32.2` to the catalog's
  downloadable lock before Git/Lake resolution. The Python setup API,
  `lean-run --with`, frontmatter, and `--lock-out` now share the same lock and
  reuse the same environment rather than creating a duplicate cache entry.

## 2.1.2 - 2026-08-12

- Raise `lean-run`'s default `--check-timeout` from 120 to 300 seconds: a
  first Mathlib check legitimately takes 2-5 minutes on slower machines
  today (instance staging plus Lake's full trace scan), and the old default
  timed out authoritative checks that would have succeeded.

## 2.1.1 - 2026-08-12

- The discovery search budget now gates starting additional candidates and
  no longer kills an in-flight authoritative check, which is bounded by its
  own per-candidate timeout. A slow machine's first Mathlib check can outlive
  the remaining search budget legitimately.
- OCI blob downloads keep partials on truncated transfers and resume with a
  Range request, retry transport errors mid-stream, and attempt four times.
- Add `build-and-publish --accelerate`: hydrate artifacts from known package
  caches (Mathlib's `lake exe cache get`) for locked packages that carry no
  artifact command of their own. Acceleration is keyed by exact canonical
  package URL, never changes the lock identity, is recorded in the hydration
  report, and the built environment is still probe-verified before
  publication. The public-cache workflow uses it, turning multi-hour Mathlib
  source builds into minutes.
- Add a fail-closed release toolkit: `scripts/registry_preflight.py` verifies
  every bundled catalog entry is anonymously downloadable, and the
  announcement-gate workflow runs the advertised PyPI journeys on clean
  unauthenticated Linux and macOS runners (dispatch + daily canary).
- `lean-run` now warns loudly when a downloadable environment is unavailable
  and states that source builds can take 30+ minutes, with a
  `--no-source-build` hint, instead of falling back silently.

## 2.1.0 - 2026-08-11

- Add `lean-runtime toolchain-slim` and
  `ToolchainManager.materialize_slim()`: a verified check-profile toolchain
  copy that drops editor indexes, static libraries, bundled LLVM/clang, and
  sources (about 2.6 GB → 2.1 GB for v4.32.2 once `--prune-original` removes
  the full Elan copy). Materialization hardlinks files, verification runs a
  capability corpus with the slim copy's own `lean`, and checking transparently
  routes through the slim copy when the full toolchain is absent. Lean v4.32
  requires all `.olean` facets and per-module IR for ordinary elaboration, so
  those remain; source builds of new environments still need the full
  toolchain.

- Exempt environment acquisition (downloads, toolchain installs, source
  builds) from the discovery search budget: a slow first-time download can no
  longer expire an otherwise healthy search. Acquisition is bounded separately
  by `DiscoveryPolicy.acquisition_timeout_seconds` and `lean-run
  --acquire-timeout`; `--search-timeout` and `--check-timeout` name the
  remaining budgets, with `--discovery-timeout` and `--timeout` kept as
  deprecated aliases.
- Split the candidate probe into `acquire` and `check` phases and announce
  first-time toolchain installation through a `toolchain.install_started`
  event and a `lean-run` progress line. `CandidateProbe` implementers must now
  provide both methods; `acquire` returns the newly exported
  `AcquiredCandidate`, and its acquisition budget is forwarded to
  `open_exact(build_timeout=...)` so `--acquire-timeout` governs source
  builds.
- Add `ExecutionResult.errors`, `.warnings`, and `.first_error` severity views,
  a `Diagnostic.location` property, and compact `repr` output for both types.
  `to_dict()` payloads are unchanged.
- Report structured diagnostics against the caller's logical input names
  (for example `Main.lean`) instead of staged sandbox paths, and rewrite the
  staged entrypoint path back to the user's file in `lean-run` text output.
  Raw `stdout`/`stderr` remain authoritative and unmodified in results.
- Add `lean.setup(toolchain="v4.32.2")` and `Runtime.open_toolchain()` for
  reusable core-only environments; the empty-`deps` error now teaches the
  toolchain form.
- Accept an exact lock path anywhere `lean-runtime profile` previously
  required an environment name (`Runtime.subject_environment`); a path that
  exists on disk always wins over an environment name.

## 2.0.9 - 2026-08-10

- Allow callers and `build-and-publish --timeout` to raise the per-step limit
  for artifact hydration and environment builds on slower platforms.

## 2.0.8 - 2026-08-10

- Add immutable, content-addressed provenance to ready-to-run program
  descriptions and accept it through `program-create --provenance-file`.
- Preserve the identity and import compatibility of existing version 1 program
  descriptions while emitting version 2 descriptions for newly created programs.

## 2.0.7

- Retry an OCI layer once from a clean partial after size or SHA-256 integrity
  verification fails, bypassing intermediary caches while preserving strict
  digest validation and emitting structured retry diagnostics.

## 2.0.6

- Preserve the highest-ranked Lean compiler rejection when automatic discovery
  exhausts its candidates, including raw output and parsed diagnostics in both
  human and JSON `lean-run` output.
- Add line-oriented and NDJSON request/response helpers to `InteractiveSession`
  for incremental REPLs and ready-to-run checker programs.

## 2.0.5

- Integrate bounded exact-environment discovery into the `lean-runtime` package.
- Make `lean-run FILE` discover standalone Mathlib environments automatically when
  no exact context or pinned Lake project is present.
- Bundle an initial deterministic catalog for core Lean v4.32.2 and Mathlib
  v4.32.2, v4.31.0, and v4.30.0, including exact Runtime locks and module inventories.
- Add `lean_runtime.discovery` for explicit planning, catalog, and authoritative
  discovery APIs, plus the `lean-runtime-catalog` maintainer command.

Version 2.0.4 was not published because its release tag retained 2.0.3 package
metadata.

## 2.0.3

- Accept portable-copy symlinks whose normalized targets remain inside the
  extracted copy, including common links such as `docs/README.md -> ../README.md`.
- Continue rejecting absolute or escaping symlink targets, duplicate archive
  members, and later archive writes that traverse an extracted symlink.

## 2.0.2

- Remove staged environments safely when Git-for-Windows leaves source pack
  files read-only or briefly locked.
- Preserve the primary environment-build diagnostic when staging cleanup also
  fails.
- Show the failed phase, command, exit code, and captured tool output when a
  verbose CLI materialization fails.

## 2.0.1

- Enable Git's long-path support for every Runtime-managed source operation so
  Mathlib environments can be prepared and materialized on Windows.
- Preserve the primary Git diagnostic when cleanup of a failed Windows source
  checkout also encounters locked files.

## 2.0.0

Version 2 gives the public interface the language used by Lean users rather
than the language of its storage implementation. The environment format stays
compatible; command names, Python names, configuration, events, and public
metadata intentionally change without aliases.

### Public terminology

- Environment libraries replace OCI caches in ordinary configuration and docs.
- Downloadable environments replace prebuilt artifacts.
- Portable copies replace OCI bundles.
- Publisher verification replaces signature-policy terminology.
- Cleanup and storage replace garbage-collection and blob terminology.
- Ready-to-run programs replace execution-capsule and container terminology.

### Main migrations

- `Runtime(caches=..., prebuilt=...)` becomes
  `Runtime(libraries=..., availability=...)`.
- `resolve`, `ensure`, and named `open` become `prepare`, `open_exact`, and
  `environment` in the explicit Python API.
- `export_environment` and `import_environment` become `save_portable_copy`
  and `open_portable_copy`.
- CLI workflows use `prepare`, `open`, `download`, `build-and-publish`,
  `save-copy`, `open-copy`, `compare`, `storage`, and `clean`.
- Environment libraries accept friendly `ghcr.io/owner/name` locations; the
  OCI transport remains an advanced implementation detail.
- Ready-to-run programs can be created, verified, copied, downloaded from a
  program library, published for multiple kinds of computers, and interrupted
  during interactive execution.

## 1.0.0

Lean Runtime v1 establishes the concise `lean-run` and `lean.setup()` workflows while making
exact environments independently verifiable, explainable, measurable, and portable.

### Breaking changes

- Remove `lean-runtime audit`, `Runtime.audit()`, `AuditReport`, and `ArtifactInventory`;
  `verify` is the sole trust surface and `verify --rebuild` performs independent rebuild checks.
- Close and version the seven public CLI JSON schemas. Execution payloads now include stable
  phase timings, while `inspect` and `gc` use one canonical data shape each.
- Remove transitional compatibility surfaces introduced before v1; callers should use the
  top-level façade, `Runtime`, and the documented v1 result types directly.

### Release capabilities

- Add lazy `setup`, `check`, `check_file`, and `replay` Python façade functions.
- Add `lean-run` with strict TOML frontmatter, automatic local-project discovery,
  exact lock input/output, concise progress, and structured JSON output.
- Add exact `mathlib@REVISION`, `leancert@REVISION`, and
  `owner/repository@REVISION` package references without permitting floating aliases.
- Add `ExecutionResult.raise_for_error()` and structured `LeanCheckError` failures.
- Rewrite the README around the front-facing workflow and expand the standalone
  file, CLI, Python, and routing documentation.
- Discover pinned local Lake projects from contained Lean files and expose a
  distinct mutable `ProjectEnvironment` API.
- Preserve project-relative file checks and record content, configuration,
  manifest, and Git project provenance without claiming an environment identity.
- Add transparent OCI prebuilt-cache lookup with authenticated registry pulls,
  disk-backed blob reuse, strict fallback policy, and explicit `pull` support.
- Add deterministic OCI publishing through `build-and-push`, with blobs and the
  platform manifest committed before the lock-level index tag.
- Stream bundle layers and OCI archives through temporary files instead of
  materializing multi-gigabyte package layers in memory.
- Add deterministic OCI image-layout export and verified, atomic environment import.
- Verify bundle digests, lock and environment identities, package Git trees,
  archive paths, host compatibility, and a Lean probe before publication.
- Separate artifact compatibility identity from informational host metadata and
  bump the environment store identity schema.
- Add verification, decision explanations, semantic context diffs, repeated profiles, and
  bounded matrix execution over ordinary execution results.
- Add interruptible toolchain installation, Lake resolution, environment builds, matrix checks,
  and project checks with process-group cleanup on cancellation and Ctrl-C.
- Ship the public JSON schemas in wheel and source distributions and expose `schema_path()`.
- Add reproducible case-study fixtures, clean-wheel smoke testing, and installed-wheel Lean
  acceptance in CI.

## 0.6.0

- Discover packages declared with either `lakefile.toml` or `lakefile.lean`.
- Translate Lake DSL configurations through the package's exact declared Lean
  toolchain instead of parsing Lean source or guessing package metadata.

## 0.5.0

- Add `Environment.execute()` for generic commands such as `lake exe`.
- Add managed `InteractiveSession` processes with live UTF-8 standard-I/O pipes.
- Run interactive tools in disposable environment clones with exact provenance.
- Enforce local timeout, memory, CPU, and bounded-transcript policies for sessions.
- Gracefully close sessions with stdin EOF before process-group termination.
- Persist final interactive `ExecutionResult` records and clean up instances.

## 0.4.0

- Add `github:owner/repository@tag-or-commit` package references.
- Discover package identity, root module, and Lean toolchain from Lake projects.
- Pin convenience references to exact commits before environment resolution.
- Add one-shot `lean-runtime check FILE --with REFERENCE` execution.
- Add `Runtime.spec_from_references()`, `resolve_references()`,
  `ensure_references()`, and `Runtime.check(..., packages=[...])`.
- Detect incompatible discovered toolchains across multi-package requests.
- Avoid Elan's implicit toolchain installation during installation checks.

## 0.3.0

- Add safe multi-file checking and replayable multi-file captures.
- Add cancellable native asyncio helpers.
- Add structured lifecycle progress events.
- Resolve friendly Git tags into exact commit locks.
- Add installation health, cache status, environment listing, and richer inspection.
- Add a scheduled real-ecosystem compatibility profile.
- Store compact, content-verified one-commit source snapshots.
- Treat direct and transitive package toolchain files as compatibility signals,
  while making the actual selected-toolchain build authoritative.
- Use execution leases so batch checks can clone concurrently without racing GC.
- Build imported locked-package roots on demand inside execution workspaces.
- Add release automation for PyPI trusted publishing.
- Build and deploy the documentation site through GitHub Pages.

## 0.2.0

- Introduce content-addressed environments, exact Git locks, offline reopening,
  execution provenance, captures, aliases, garbage collection, and trusted local
  resource policies.
