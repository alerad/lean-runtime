# Local Lake projects

Lean Runtime discovers the nearest pinned Lake project from the current
directory or source path.

```bash
lean-runtime check
lean-runtime check MyProject/Basic.lean
lean-runtime build
lean-runtime watch MyProject/Basic.lean
```

Focused checking passes the real project-relative path to Lake. Project-wide
checking builds local libraries' Lean artifacts; `build` retains ordinary Lake
build semantics and root outputs.

## Create

```bash
lean-runtime new MyProof
```

`new` only creates projects. It chooses the stable cataloged Mathlib/toolchain,
creates exact Lake metadata, shares exact dependency storage, and optionally
generates CI. It refuses an existing Lake project; use `adopt` there.

## Adopt an existing project

```bash
cd ExistingProject
lean-runtime adopt
```

Adoption does not update the toolchain or manifest. It validates pinned Git
dependencies, checks dirty/mismatched checkouts, uses the existing
`.lake/packages` as an exact byte donor, prepares the shared graph, probes it,
then atomically replaces package directories with links. The old graph is
restored if any post-swap probe fails.

For many repositories:

```bash
lean-runtime adopt ~/research
```

A directory that is itself a Lake project is adopted as one project; otherwise
Lean Runtime discovers pinned projects below it. Independent failures do not
prevent safe projects from being processed.

Advanced equivalents are `project scan`, `project share`, and `project
unshare`. They also default to the current directory.

## Existing Elan

If the exact pinned toolchain already exists in the user's Elan home, Lean
Runtime invokes its `lean`/`lake` binaries read-only. It never changes the user
default, installs there, or prunes it. Missing toolchains go to the private
runtime store. `toolchain optimize` can create a verified slim checking copy;
pruning is allowed only for a private original.

## Update

```bash
lean-runtime update
```

The command shows the exact old/new Mathlib commit and toolchain, local reuse,
and download requirements before confirmation. Application is transactional;
project metadata is restored if the new graph or project-wide check fails.
Use `--dry-run`, `--offline`, or `--yes` when scripting.
