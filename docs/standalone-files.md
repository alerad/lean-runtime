# Standalone Lean files

`lean-run` chooses one execution context using the following precedence:

1. `--lock` or a frontmatter `lock`;
2. `--with` or frontmatter `requires`;
3. an explicit `--toolchain` or frontmatter `toolchain`;
4. the nearest pinned local Lake project discovered from the file;
5. an actionable missing-context error.

Conflicting CLI and frontmatter declarations are rejected. Imports are never
used to guess repositories or versions because a Lean module name does not
identify its package source or compatible dependency graph.

## Frontmatter format

The block must appear before Lean source and uses TOML encoded in Lean comments:

```lean
-- /// lean-runtime
-- requires = ["mathlib@v4.32.2", "leancert@v4.32.2.4"]
-- toolchain = "leanprover/lean4:v4.32.0"
-- ///
```

Supported fields are:

- `requires`: an array of exact friendly package references;
- `toolchain`: an optional compatibility override for those dependencies, or
  the pinned toolchain for a core-only file;
- `lock`: an exact lock path, resolved relative to the Lean file.

`lock` cannot be combined with `requires` or `toolchain`. Unknown fields, malformed TOML,
non-comment content inside the block, and late frontmatter are errors.

## Lock output

`--lock-out PATH` is valid only with dependency declarations. It resolves the
graph, writes the canonical lock, ensures the environment, and still checks the
file. It cannot be combined with an exact input lock.

## Output

Human output is concise:

```text
✓ Main.lean accepted in 0.42s
```

Cold operations report meaningful dependency, cache, and build phases on
stderr. `--quiet` suppresses progress and `--json` emits the complete structured
`ExecutionResult` without progress messages.
