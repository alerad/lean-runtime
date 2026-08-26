# Context selection

Every check runs inside one exact **environment**. The **context** is where that
environment comes from. Selecting a context and Lean accepting the source are
separate decisions: discovery proposes an environment, and only Lean accepts it.

For ordinary standalone checks, let Lean Runtime discover the environment.
Explicit selection is an override for reproducibility, compatibility testing,
or a request that specifically names a release.

## Precedence

For a Lean file, Lean Runtime considers context sources in this order:

1. An explicit command-line or Python API selection
2. Lean Runtime frontmatter in the file
3. The nearest pinned Lake project that owns the file
4. Automatic catalog discovery

If none can produce an environment, the operation fails before Lean runs.

The first three sources are **exact**: they name one environment outright. The
fourth is only a **proposal** until Lean accepts the file inside a candidate.
`status` reports which of the two it is.

## Explicit selection

```console
lean-runtime check Main.lean --using mathlib@v4.33.0
```

`--using` can identify a project directory, a lock file, a stored environment, a
toolchain, or a package release. Explicit input does not need discovery.

## Frontmatter

```lean
-- /// lean-runtime
-- toolchain = "leanprover/lean4:v4.33.0"
-- ///
example : 1 + 1 = 2 := by decide
```

Frontmatter travels with the source while remaining valid Lean comments.

## Pinned project context

A file declared beneath a target in a `lakefile.toml` project uses the nearest
project with a pinned toolchain. A file that is merely stored under that project,
but outside every declared target root, proceeds to automatic discovery. For an
imperative `lakefile.lean`, where ownership cannot be established without running
project code, Lean Runtime conservatively preserves ancestry-based selection.
Lean Runtime does not replace project version decisions during a normal project check.

To explicitly ignore every ancestor project, use:

```console
lean-runtime check Main.lean --standalone
```

`--standalone` cannot be combined with `--using` because they are competing
context-selection instructions.

## Automatic discovery

Discovery begins with declared source imports. The bundled catalog associates exact environments with module inventories. Plausible candidates are ordered under a bounded policy and checked in turn.

Static analysis proposes candidates. It does not prove compatibility. A candidate is accepted only if Lean accepts the source inside that exact environment.

Candidate count, compiler time, wall time, remote acquisition, offline mode, and source-build permission can bound the search. Reaching a bound can produce an inconclusive discovery result rather than a claim that no compatible environment exists.

## Inspect the decision

```console
lean-runtime status Main.lean
lean-runtime status Main.lean --probe
lean-runtime status Main.lean --json
```

`status` is a dry run of `check`. For a standalone file it reports the context
source, its confidence (`proposed` for discovery, `exact` for a project, lock, or
stored environment), the imports used as evidence, the candidate environments in
the order `check` would try them, whether each environment and toolchain is
already local, and — with `--probe` — the size of the first download. It also
names an ancestor project that was ignored because no declared target owns the
file. `status` never runs Lean and never acquires anything.

A successful `check` reports the verdict together with the environment that
produced it, and its `--json` provenance records the exact identities involved.
