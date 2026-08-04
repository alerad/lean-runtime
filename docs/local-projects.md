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
lean-runtime raw-check ./my-project/MyProject/Main.lean
```

The actual project-relative file is passed to `lake env lean`, so imports of
local modules retain normal Lake semantics. Checking a source string writes a
uniquely named disposable file under `.lake/lean-runtime/` and removes it after
execution:

```python
result = project.check("import MyProject\nexample : True := by trivial")
```

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

Lean Runtime does not run `lake update`, rewrite the manifest, or copy the
checkout into its immutable store. `project.build()` explicitly runs the pinned
toolchain's `lake build`; checks otherwise use the project exactly as it exists.
As with managed environments, Lake configuration and dependencies are trusted
code and the local backend is not a sandbox.
