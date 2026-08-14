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

The CLI equivalent is:

```bash
lean-runtime check-file ./my-project/MyProject/Main.lean
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
each repository's `.lake/packages`. If many projects pin the same graph, use a
shared build instead:

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
sources are fetched at the exact manifest commit. Lean Runtime does not delete
the old `.lake/packages`; after verifying the shared build, those old copies can
be removed separately if desired. `lean-runtime storage` reports shared project
package usage; automatic cleanup of those packages is not yet implemented.

This mode requires a manifest and never runs `lake update`, so it cannot silently
change dependency revisions. It also reuses an existing local Git object database
when that database contains another requested revision, avoiding redundant Git
downloads. Local `path` dependencies remain at their declared locations and are
included in the workspace identity. Use `lean-runtime build . --shared` after
changing the manifest or a local path dependency.

This differs from `lake env lean File.lean`: that command only constructs the
current workspace's environment and checks one file; it neither builds missing
imports nor shares dependencies with another checkout. The shared build still
uses Lake's real build graph and project targets—it only changes where locked
dependencies come from.

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
