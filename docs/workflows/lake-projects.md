# Work with Lake projects

Lean Runtime uses the project toolchain and `lake-manifest.json` as the authority for mutable Lake projects.

## Inspect a project

From the project root:

```console
lean-runtime status .
lean-runtime project info .
```

`status` reports the project context: it is **exact** — the pinned toolchain and
`lake-manifest.json` name the environment outright, so nothing is proposed or
discovered. `project info` provides project-specific
storage and dependency information. It exits successfully when inspection succeeds;
publication blockers, if any, appear under `Ready to publish: no`.

## Adopt shared dependency storage

Preview adoption before changing project package paths:

```console
lean-runtime adopt . --dry-run
```

Apply it explicitly:

```console
lean-runtime adopt . --yes
```

Adoption reads the pinned toolchain and manifest, registers exact package revisions, and prepares managed package paths. Sharing can be reversed:

```console
lean-runtime project unshare . --yes
```

## Check the project

```console
lean-runtime check
lean-runtime check MyProject/Basic.lean
```

With no path, `check` uses the current project. A file inside the project uses the nearest pinned project context unless an explicit context overrides it.

## Build the project

```console
lean-runtime build
lean-runtime build MyTarget
```

Before invoking Lake, `build` may restore artifacts through a known dependency cache accelerator. Mathlib projects can use `lake exe cache get` when the dependency graph supports it. Hydration failure is recorded and the Lake build continues from source.

Skip cache hydration when required:

```console
lean-runtime build --no-cache
```

## Dependency reuse

Project package sources can be shared at exact revisions. Compiled artifact reuse additionally depends on the toolchain, platform ABI, package configuration, and the relevant transitive dependency cone.

Unrelated packages elsewhere in a project graph do not change a package's own dependency cone. A revision or toolchain mismatch prevents compiled artifact reuse, though compatible local Git objects may still reduce network transfer.

Reuse is keyed on the resolved commit. A requested tag such as `v4.33.0.1` is
shown in diagnostics as provenance but never participates in identity, so two
projects requesting the same mutable tag share packages only when that tag
resolved to the same commit.

## Update safely

`update` moves a locked Mathlib project to the latest cataloged stable Mathlib and
matching toolchain. Projects without a cataloged Mathlib dependency have nothing to
update and report a successful no-op.

Preview the update plan:

```console
lean-runtime update . --dry-run
```

Apply the update:

```console
lean-runtime update . --yes
```

Use `--offline` when all required update information is already available locally.

### Bulk adoption and reuse compatibility

Adoption planning uses a bounded pool for project inspection and unique local
source hashes. The default chooses up to eight workers from available CPUs and
RAM, reserving one CPU and 512 MiB and budgeting 512 MiB per worker. Linux process
affinity and standard cgroup-v2 limits are considered. If available memory cannot
be detected, it falls back to one worker. This is a sizing heuristic, not a hard
memory limit.

Use `lean-runtime adopt DIRECTORY --dry-run --jobs 2` to override the worker count;
`--jobs 1` runs planning serially. The selected count is shown in progress and in
the plan's `jobs` JSON field. Repeated references to the same resolved source
directory are hashed once per plan. Results retain discovery order. Cached hashes
and size inventories are discarded after each plan; execution still revalidates.
Package preparation, Lake probes, and attachment/link swaps remain serialized.

Preview a directory with `lean-runtime adopt DIRECTORY --dry-run`. Discovery
includes nested Lake projects and excludes `.git` and `.lake`. Adoption processes
projects individually; a failed project does not roll back earlier successes.
Rerunning adoption validates each attachment against the current graph and repairs
stale links through the normal adoption path. `lean-runtime verify PROJECT` checks
attachment metadata, exact identities, and links locally without repairing them or
installing tools. Its JSON report has `subject_kind: project`, null lock/environment
IDs, and does not claim a Lean proof verdict.

Shared packages use the entire normalized root-resolved graph as a conservative
compatibility boundary, together with source and exact Lean/Lake build identities.
Different toolchains or resolved dependency graphs remain separate. Even an
unrelated dependency change can require another shared package variant. Older
compatibility records can donate source but cannot establish artifact compatibility.

First adoption evaluates the original local checkout separately as an artifact
donor. Build outputs are migrated only with matching recorded provenance; clean
Git sources alone do not prove which compiler built an `.olean`. Missing or
incompatible provenance is reported as rejected and requires rebuilding or cache
restoration. Only the build `lib`, `ir`, and `bin` trees are migrated; package links,
configuration caches, symlinked outputs, `.trace`, and `.hash` files are excluded.
A successful graph probe is not a full Lean build or proof check.

### Operation costs

Planning never installs a toolchain. It uses the same source/donor selection as
preparation, while execution repeats that selection against current files and
cache state. JSON includes `actions` and a versioned `estimates` object
(`lean-runtime-adoption-estimates/1`). Actions identify compatible shared reuse,
local import, cached source, local Git-object cloning, exact fetch, or unresolved
compatibility. Artifact preservation is a separate decision.

The report separates these quantities:

- **Local directories replaced:** regular-file logical bytes, without following
  dependency links. This does not establish reclaimed disk allocation.
- **New retained content:** source objects, package source trees, eligible preserved
  outputs, and unpriced workspace/probe metadata. One source object is charged
  once across package actions; source objects and package trees are separate
  logical copies. Rejected outputs are omitted, not charged as a future build.
- **Payload transfer:** exact known acquisition units only. Git and unavailable
  toolchain transfers remain explicitly unpriced. Uncompressed artifact sizes
  never become download estimates. Sparse capsule planning and pulling share
  missing-frame validation and deduplicate exact compressed ranges. Config bytes
  read during planning are recorded separately from remaining payload.
- **Disk capacity:** physical recovery and additional working space remain unknown.
  Reflinks, hardlinks, staging, compression, and exclusive allocation require
  evidence that logical inventories do not supply.

Cost components have action coverage, quantity, evidence, and completeness.
Unreadable inventory entries leave the affected component incomplete. Signed
`net_logical_change_bytes` can express growth; it is null when required components
are unpriced. The partial `known_logical_change_bytes` is not a final forecast.
Typed uncertainty groups unique toolchains/source identities and affected projects;
`--verbose` shows paths, donor decisions, and provenance.

Legacy scalar fields remain for compatibility: `new_source_bytes` is the source
object portion of known `new_shared_bytes`. `estimated_machine_reclaimable_bytes`
and its alias are always null: logical subtraction cannot establish physical
recovery. `approximate_artifact_bytes` is deprecated and null. These are additive
JSON changes, not a redefinition of logical bytes as allocated bytes.

Attach/detach results include local inventory observations and backup cleanup status.
Backup cleanup occurs after transaction commit. Cleanup failure reports the retained
backup and does not roll back an already committed attachment or detachment.
Observations describe local directory content, not total machine recovery.

### Published size references

Only unresolved artifact-preservation decisions request advisory samples. Source-only
adoption does not acquire or price hypothetical published builds. When OCI libraries
are enabled, reference lookup reads verified manifests/configs for relevant repository
and subdirectory requests, without installing compilers or downloading packs.
`--availability local` disables these registry lookups.

Selection prefers the same source revision, then the same compiler release and
nearby releases. Different-revision samples more than two minor compiler releases
away are declined. References cover published **check** artifacts only, excluding
sources, full toolchains, and native/development outputs. They never change pins,
compatibility, or execution decisions.

The default preview mentions available references; `--verbose` groups package size
ranges and shows provenance. JSON retains `size_references`, with action coverage
and `included_in_totals: false`. Neither multiplying one sample by the compatibility
variant count nor counting each sample once predicts storage: actual content overlap
is unknown. References are excluded from all storage and transfer totals.
