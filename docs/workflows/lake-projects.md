# Work with Lake projects

Lean Runtime uses the project toolchain and `lake-manifest.json` as the authority for mutable Lake projects.

## Inspect a project

From the project root:

```console
lean-runtime status .
lean-runtime project info .
```

`status` reports the selected project context. `project info` provides project-specific
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
