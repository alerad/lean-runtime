# Standalone Lean files

```bash
lean-runtime check Main.lean
```

Outside a pinned Lake project, `check` analyzes imports and syntax, ranks a
bounded catalog of exact environments, acquires a verified capsule or builds
from exact source according to policy, and runs Lean with logical paths in its
diagnostics. Inside a project, the same command uses Lake project semantics.

Put durable context in the file:

```lean
-- /// lean-runtime
-- requires = ["mathlib@v4.33.0"]
-- ///
```

or override once:

```bash
lean-runtime check Main.lean --using mathlib@v4.33.0
lean-runtime check Main.lean --using lock:environment.lock.json
lean-runtime check Main.lean --using toolchain:v4.33.0
```

`--offline` is fail-closed. `--using env:NAME` selects an already-opened named
environment. Frontmatter rejects malformed TOML, unknown fields, late blocks,
and conflicting selectors before acquisition.

Use `watch FILE` for edit/check loops, `--repeat N` for repeated measurement,
and `--matrix [FILE]` for compatibility checks.
