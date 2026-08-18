# Check a single file

The zero-setup path: no project, no configuration — the file's imports
are enough.

```bash
lean-runtime check Main.lean
```

Outside a Lake project, `check` reads the imports, picks the best exact
environment from a curated catalog, fetches only what the file uses, and
runs Lean. Errors and warnings point at your filename and line numbers,
just like a local build. (Inside a Lake project, the same command uses
your project instead — see [Lake projects](local-projects.md).)

## Pin the version in the file

If the file should always check against one Mathlib release, say so in a
frontmatter block:

```lean
-- /// lean-runtime
-- requires = ["mathlib@v4.33.0"]
-- ///
```

Malformed or conflicting frontmatter is rejected up front with a clear
error, before anything is downloaded.

## Or pin it for one run

```bash
lean-runtime check Main.lean --using mathlib@v4.33.0
lean-runtime check Main.lean --using lock:environment.lock.json
lean-runtime check Main.lean --using toolchain:v4.33.0
```

`--using env:NAME` selects an environment you've already set up by name,
and `--offline` guarantees no network access — missing pieces become
errors, never silent downloads.

## Keep the loop going

```bash
lean-runtime watch Main.lean                      # re-check on save
lean-runtime check Main.lean --repeat 5           # repeated timing samples
lean-runtime check Main.lean --matrix matrix.toml # several Mathlib versions at once
```

See [Verify, compare, and measure](v1-precision.md) for matrix files and
timing details.
