# Local Lake projects

Lean Runtime can discover and operate on an existing trusted Lake checkout
without converting it into an immutable managed environment:

```python
from lean_runtime import Runtime

project = Runtime().project("./my-project")
project.build()
result = project.check_file("./my-project/MyProject/Main.lean")
```

`Runtime.project()` accepts either the project root or any contained path. It
walks upward and selects the nearest directory containing both `lean-toolchain`
and `lakefile.toml` or `lakefile.lean`.

`Runtime.check_file(path)` performs the same discovery automatically when no
managed environment, dependency list, or explicit toolchain is supplied:

```python
result = Runtime().check_file("./my-project/MyProject/Main.lean")
```

The primary CLI equivalent is:

```bash
lean-runtime check ./my-project/MyProject/Main.lean
```

The actual project-relative file is passed to `lake env lean`, so imports of
local modules retain normal Lake semantics. Checking a source string writes a
uniquely named disposable file under `.lake/lean-runtime/` and removes it after
execution:

```python
result = project.check("import MyProject\nexample : True := by trivial")
```

## Reusing dependencies across projects

Ordinary Lake workspaces put remote dependencies and their build artifacts in
each repository's `.lake/packages`. New projects can start in shared mode:

```bash
lean-runtime init MyProof
cd MyProof
lean-runtime check MyProof/Basic.lean
lean-runtime check
lean-runtime build
```

The fileless form asks Lake for the project's declared local libraries and
builds their `leanArts` facets. Lake therefore expands roots and submodules,
orders local imports, and creates any intermediate local `.olean` files; Lean
Runtime does not parse module globs or implement a second build scheduler. It
does not request executable or native-library targets. The Python equivalent is
`Runtime().project(".").check_all()`.

Projects created by `init` on a supported Lake also opt their root package into
Lake's native artifact cache. Lean Runtime capability-probes the resolved Lake executable
once, stores the result per exact toolchain and platform ABI, and otherwise
stays silent. Only root-project outputs enter this cache: locked dependencies
continue to use the verified shared workspace, avoiding a second multi-gigabyte
copy. `build` retains Lake's complete target semantics.

The generated files are a standard Lake project plus a small
`lean-runtime.toml`. Root build outputs stay local; exact dependencies are
shared. The newest stable cataloged Mathlib is selected by default. Select a
release with `--mathlib 4.33.0`, or use `--core` for a core-only library.
`init` also creates an `AGENTS.md` describing the safe
build and dependency workflow unless `--no-agents` is passed; it never
overwrites an existing guide.

Initialization acquires and verifies the exact graph before publishing the
target directory. Use `--plan` for a side-effect-free cost report, `--offline`
to require local data, `--max-download SIZE` to enforce a transfer ceiling, or
`--seed-from PROJECT` to name an exact local donor.
Offline planning never queries configured registries. A missing exact local
graph appears as a blocker and makes the planning command fail.
The plan separately reports whether the full Lake-capable toolchain is already
installed. Because Elan does not publish a preflight byte count, a missing full
toolchain is rejected under `--offline` or `--max-download` rather than silently
bypassing the policy.

An otherwise empty Git root is a valid new-project target. The original `.git`
directory—or `.git` worktree file—stays in place without changing HEAD, the
index, or remotes. The existing directory inode also stays live, so invoking
`lean-runtime init .` does not strand the shell in an unlinked working
directory. A pre-existing `AGENTS.md` is preserved. Other contents must be moved
first or represented by an existing pinned Lake project; the read-only plan
enforces the same rule as execution.
Use `--name NAME` when creating inside a lowercase or otherwise differently
named repository and the Lean root module needs an explicit spelling.

For an existing pinned project, `lean-runtime init .` adopts its current
manifest without changing versions. Register a collection once with
`lean-runtime scan ~/research`; future exact matches are preferred over
downloads automatically.

Move an adopted TOML project to the newest cataloged Mathlib explicitly:

```bash
lean-runtime update --plan
lean-runtime update
```

The plan reports old and new release, commit, toolchain, local donor, and known
download bytes. Acquisition happens before metadata changes, and failures
restore the prior Lake files and attachment metadata.

For advanced bulk onboarding, preview before changing anything:

```bash
lean-runtime attach .
lean-runtime attach . --execute
```

For a directory containing many projects:

```bash
lean-runtime attach ~/research --recursive
lean-runtime attach ~/research --recursive --execute
```

The preview groups exact graphs and separately reports checkout bytes removed,
compatible shared bytes already ready, new shared bytes required, and estimated
machine-level recovery. It also identifies missing local paths, dirty
dependencies, and revision mismatches. Execution continues through independent
projects while reporting per-project failures. It first prepares the exact
shared workspace and probes it through Lake. Only then does it atomically
replace `.lake/packages` with package links, probe the resulting project through
ordinary Lake, and discard the old generated copies. Any failure restores the
original package directory.

The project itself remains portable. To return to independent package copies:

```bash
lean-runtime detach .
lean-runtime detach . --execute
```

Detachment copy-on-write clones the exact packages where the filesystem allows,
probes the standalone graph, and only then removes the attachment metadata. The
preview reports the maximum independent-copy size and available disk space;
execution refuses to rely on copy-on-write support when there is not enough
space for a full fallback copy.

The lower-level opt-in remains available without changing project layout:

```bash
lean-runtime build . --shared
```

Or from Python:

```python
Runtime().project(".").build(shared=True)
```

Lean Runtime reads the existing `lake-manifest.json` and passes Lake a generated
`--packages` override. Each remote package is keyed by its exact revision,
effective transitive dependency closure, Lean toolchain, and host platform;
local-path dependency contents are part of the surrounding workspace identity.
Dependency sources and their `.lake/build` artifacts live under the runtime
store, while the root project's own `.lake/build` remains in the checkout. This
lets two different root manifests share the same Mathlib package safely. Builds
that touch any of the same package keys are serialized because Lake may update
their artifacts.

The first shared build imports a clean, revision-matching local dependency copy
when one exists, using copy-on-write filesystem clones where supported. Missing
sources are fetched at the exact manifest commit. A later project bypasses
source resolution when a managed package marker proves that its toolchain,
platform, revision, and effective dependency closure already match. A plain shared build never
deletes the old `.lake/packages`; only an explicit `attach --execute` replaces
those generated copies after verification. `lean-runtime storage` reports
shared project package usage; automatic cleanup of those packages is not yet
implemented.

Check capsules and mutable shared packages are intentionally separate storage
tiers. Capsules are source-free and trimmed for checking; ordinary Lake
development needs source-shaped packages and build metadata. A capsule can seed
compatible build artifacts while the exact source workspace is materialized,
but attached projects ultimately link to the mutable shared-project store.

This mode requires a manifest and never runs `lake update`, so it cannot silently
change dependency revisions. It also reuses an existing local Git object database
when that database contains another requested revision, avoiding redundant Git
downloads. Local `path` dependencies remain at their declared locations and are
included in the workspace identity. Attached projects automatically use shared
mode when run through `lean-runtime build`; `--local` is an explicit escape
hatch for unattached checkouts. An attached project must be detached before a
local build, so `--local` cannot silently continue using shared links. After
changing the manifest or a local path dependency, rerun `lean-runtime attach .
--execute` to refresh the ordinary-Lake links.

This differs from `lake env lean File.lean`: that command only constructs the
current workspace's environment and checks one file; it neither builds missing
imports nor shares dependencies with another checkout. The shared build still
uses Lake's real build graph and project targets—it only changes where locked
dependencies come from. Attached links also let ordinary `lake build` and the
editor load the graph; prefer `lean-runtime build` when several projects may
build concurrently, because it serializes writes to shared artifacts.

## Mutable-project provenance

A local project is intentionally represented by `ProjectEnvironment`, not the
content-addressed `Environment` used for locked dependency graphs. Its results
therefore have `environment_id=None` and include project provenance containing:

- the canonical project root;
- a content digest excluding `.git` and `.lake`;
- Lake configuration and manifest digests;
- the Git revision and dirty state when Git metadata is available.

The workspace digest changes when local source or configuration changes, while
generated Lake artifacts do not affect it.

Lean Runtime does not run `lake update` or rewrite the manifest.
`project.build()` explicitly runs the pinned toolchain's `lake build`; checks
otherwise use the project exactly as it exists. Shared builds copy or fetch only
locked dependencies into the runtime store and leave the mutable root checkout
in place.
As with managed environments, Lake configuration and dependencies are trusted
code and the local backend is not a sandbox.
