# Standalone Lean files

`lean-run` chooses one execution context using the following precedence:

1. `--lock` or a frontmatter `lock`;
2. `--with` or frontmatter `requires`;
3. an explicit `--toolchain` or frontmatter `toolchain`;
4. the nearest pinned local Lake project discovered from the file;
5. bounded discovery from the bundled exact-environment catalog;
6. an actionable discovery or missing-context error.

Conflicting CLI and frontmatter declarations are rejected. Imports filter and
rank curated exact catalog entries; they are never treated as proof that an
environment is compatible. Runtime materializes each bounded candidate and
Lean authoritatively checks the source before discovery can succeed.

The bundled bootstrap catalog contains core Lean v4.32.2 plus exact Mathlib
v4.32.2, v4.31.0, and v4.30.0 locks. `--catalog PATH` selects another validated catalog,
`--max-candidates` and `--discovery-timeout` bound the search, and
`--no-discover` restores strict explicit-context behavior.

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

`--lock-out PATH` is valid with dependency declarations or automatic discovery.
It writes the canonical successful lock, ensures the environment, and still
checks the file. It cannot be combined with an exact input lock or a mutable
local project.

Discovery tries local and downloadable environments before a source build.
`--no-source-build` forbids that fallback; `--offline` permits retained local
environments only.

## Output

Human output is concise:

```text
✓ Main.lean accepted in 0.42s
```

Cold operations report meaningful dependency, cache, and build phases on
stderr. `--quiet` suppresses progress and `--json` emits the complete structured
`ExecutionResult` without progress messages.
