# Many projects, bounded storage

Keep each project's `lean-toolchain` and `lake-manifest.json` authoritative.
Sharing dependencies does **not** merge incompatible revisions into one environment.

## Inspect before adopting or downloading

```sh
lean-runtime readiness ~/research/formalizations --recursive --json
lean-runtime readiness /path/to/first /path/to/second --json
```

This new command executes neither Lean nor Lake, performs no network access,
and creates no runtime store, registrations, project files, or lock files.
Git queries disable optional index-refresh writes. It reports:

- selected toolchain capabilities: **check-ready** versus **build-ready**;
- missing exact cached dependency revisions and missing path dependencies;
- toolchain and overlapping Git-pin conflicts in path dependencies;
- incompatible package variants across projects, separately as `pool_conflicts`;
- a `shared_adoption_preview`, reusing adoption's deduplicated action/cost planner;
- incomplete/unknown storage estimates explicitly, rather than assuming zero.

`inputs_ready` means the inspected inputs are local, **not** that the project
compiles. Compiled artifact compatibility is explicitly unverified; build output
size is unknown. A Lean verdict requires running a check/build. Differing pins in
independent projects are valid; pool conflicts are not automatically project errors.
Exit 0 means all inspected project inputs are ready; exit 2 means some are not
(or no projects were found). The JSON report has no Lean verdict.

Existing `adopt` remains a separate preview/apply workflow. Readiness never adopts.

## Enforce local acquisition policy

```python
from lean_runtime import Runtime

runtime = Runtime(availability="local", libraries=[])
capabilities = runtime.toolchains.local_capabilities("4.33.1")
print(capabilities["check_ready"], capabilities["build_ready"])
```

The policy reaches the underlying toolchain manager, including `ensure`,
`ensure_full`, `command`, and Elan bootstrap. A verified slim toolchain supports
checking but does not satisfy Lake builds. Missing full tools are refused before
installation. Missing source fetches, reference discovery, and new Lake graph
resolution also fail closed; use a retained exact lock or cataloged reference.
Automatic dependency artifact download helpers are skipped locally.

`toolchain info VERSION` is now inspection-only, even when VERSION is absent.
Use `toolchain install VERSION` explicitly to acquire a full installation.

Local acquisition policy is **not an OS sandbox**. Authored Lean/Lake code and
explicit trusted-local commands can have arbitrary effects. Use a sandbox backend
and disabled network policy when executing untrusted code. A manager tightened by
a local Runtime stays local; use separate managers for different acquisition policies.

## Borrow toolchains without depending on HOME

```sh
lean-runtime --borrowed-elan-root /Users/me/.elan readiness /path/to/project --json
```

```python
runtime = Runtime(
    availability="local",
    borrowed_elan_roots=["/Users/me/.elan", "/another/existing/elan-home"],
)
```

Roots are Elan **homes**, containing `toolchains/`, not individual installations.
The flag is repeatable. `LEAN_RUNTIME_BORROWED_ELAN_ROOTS` accepts a platform
path-separator-delimited list when explicit roots are not supplied. Explicit
borrowed roots work even with a private provider HOME or explicit private Elan
configuration. Private installations take precedence, then supplied borrowed
roots, then ordinary detected user Elan. Capability reports identify the selected
path and its private/borrowed ownership. Borrowing does not copy toolchains or
make them eligible for Runtime's `prune_original` operation.

## Bound acquisition and build growth

```sh
lean-runtime build --max-download 128MiB --max-dependency-bytes 256MiB --max-build-bytes 128MiB
```

```python
from lean_runtime import Runtime, StorageLimits

runtime = Runtime(
    availability="local",
    storage_limits=StorageLimits(
        toolchain_bytes=256_000_000,
        dependency_bytes=512_000_000,
        build_bytes=128_000_000,
    ),
)
result = runtime.build("/path/to/project")
```

CLI flags `--max-toolchain-bytes`, `--max-dependency-bytes`, and
`--max-build-bytes` are also accepted before the command. Check's standalone
context translation preserves them. `StorageLimitExceeded` is an infrastructure
failure (`reason_code="storage_limit_exceeded"`), not a Lean rejection.

Limits are operation-scoped **net new logical bytes**, not total existing cache
sizes or exclusive physical allocation. Known tree copies are priced before
copying; cache hits cost no new bytes. Guarded paths do not follow symbolic links
or Windows junctions and count hardlinked files once across the inventory.

- Toolchain acquisition checks published unpacked size when available. A full
  Elan installation has no reliable preflight size, so acquisition is refused
  when a download/toolchain budget is set. Preinstall it explicitly instead.
- `max_download_bytes` / `--max-download` bounds OCI payload requests across a
  guarded operation. Cached blobs cost zero. Requests reserve bytes atomically,
  including conservative retry reservations; bounded registry metadata is not a
  wire-traffic quota. Git fetch/resolution sizes are unknown and refused under
  this budget. Unpriced automatic artifact-download helpers are skipped.
- Dependency limits cover managed source, package, workspace, environment,
  program, OCI, and artifact caches, including acquisition staging.
- Build limits cover the project tree, managed shared packages/workspaces, and the owned
  Lake artifact cache; disposable environment checks/builds monitor their job trees.
  They are conservative: dependency preparation within a
  project build can consume both the dependency and build allowance.

Growth is sampled at 100 ms plus cooperative checkpoints. Cancellation stops
backend process trees without setting the caller's cancellation event. These
are **not filesystem quotas**: concurrent writers, large synchronous copies,
custom outputs outside guarded paths, and writes between samples can exceed a
limit. A refusal does not automatically delete existing or newly completed cache
objects. Normal cleanup remains explicit. Failed stages use existing staged
publication cleanup; no global cache deletion is performed.

## Shared Lake configuration without duplicating library artifacts

Shared builds now create configuration-isolated dependency roots beneath their
managed workspace. Immediate source entries and `.lake/build` are linked to the
exact dependency; `.lake` itself is owned by the view. Older Lake versions can
write configuration locks/caches without requiring writable dependency sources.
First-build outputs are linked back into the shared package, so later projects
reuse them too. Workspace paths remain stable across projects using the same graph.

The low-level `lean_runtime.dependency_views.dependency_view(source, destination)`
can adapt an already-built, read-only dependency for an embedding. It creates no
source or artifact copy. On Windows, directory junctions and file hardlinks avoid
requiring symlink privileges (file hardlinks require the same filesystem).

This is a layout adapter, **not** a read-only filesystem enforcement mechanism.
Shared artifact writes still require the existing package build locks; consumers
of the low-level API must arrange their own coordination. Configuration views
are generated cache data, never authored project dependencies.
