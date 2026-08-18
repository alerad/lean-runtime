# Work in a Lake project

Lean Runtime works with your project as-is. It finds the nearest pinned
Lake project from wherever you run it:

```bash
lean-runtime check                        # whole project
lean-runtime check MyProject/Basic.lean   # one file, fast
lean-runtime build                        # ordinary Lake build
lean-runtime watch MyProject/Basic.lean   # re-check on save
```

Checking a single file passes the real project-relative path to Lake, so
diagnostics look exactly like they would from a local build. Project-wide
`check` builds your libraries' Lean artifacts; `build` keeps ordinary
Lake semantics and outputs.

## Create a project

```bash
lean-runtime new MyProof
```

`new` picks the stable cataloged Mathlib/toolchain pair, writes exact
Lake metadata, shares dependency storage, and can generate CI. It only
creates — for an existing project, use `adopt`.

## Share storage across projects

If you have several projects pinned to the same dependencies, each one
keeps its own multi-gigabyte copy. `adopt` deduplicates them:

```bash
cd ExistingProject
lean-runtime adopt
```

Adoption never changes your `lean-toolchain` or `lake-manifest.json`. It
verifies your pinned dependencies and current checkouts, shows a preview,
tests the shared copy, and only then swaps package directories for links —
atomically, with automatic rollback if any post-swap test fails.

Have a whole folder of projects?

```bash
lean-runtime adopt ~/research
```

Each project is handled independently — one problematic project doesn't
block the rest.

## Your Elan install is safe

If the exact pinned toolchain already exists in your Elan home, Lean
Runtime uses those binaries read-only. It never changes your default,
installs into your Elan home, or removes anything from it. Toolchains you
don't have go into Lean Runtime's private store instead.

## Update dependencies safely

```bash
lean-runtime update
```

Before touching anything, `update` shows the exact old and new Mathlib
commit and toolchain, what can be reused, and what needs downloading. The
change is transactional: if the new dependency graph or a project-wide
check fails, your project metadata is restored. Use `--dry-run` to just
look, `--yes` for scripts.
