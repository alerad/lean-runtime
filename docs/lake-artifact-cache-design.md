# Lake artifact-cache integration design

!!! note

    This is an implementation design and performance gate, not a description
    of functionality available in the current release.

## Ownership

Lean Runtime should complement the standard Lean tools rather than replace
their build semantics:

- Elan selects and installs the toolchain pinned by `lean-toolchain`.
- Lake resolves packages and owns targets, facets, traces, executables, custom
  build logic, and complete project builds.
- Mathlib's cache supplies prebuilt upstream artifacts but does not cache a
  downstream project's own outputs.
- Lake's artifact cache shares input- and content-addressed outputs between
  workspaces using the same toolchain and can register remote mappings for lazy
  restoration.
- Lean Runtime selects, verifies, distributes, and shares an exact environment
  with provenance and download policy.

Consequently, Lean Runtime must not implement a second general-purpose Lake
target scheduler.

## Public contract

The intended commands are:

```text
lean-runtime check Foo.lean
lean-runtime check
lean-runtime build
```

`check Foo.lean` remains the focused development loop. A file-less `check`
will check all local Lean modules without producing executables or native
libraries. `build` remains a complete Lake-compatible build of declared local
targets; it must not silently become a partial build.

## Integration boundary

`ProjectExecutor` owns project command selection and execution policy. A future
`LakeArtifactCache` service should be injected into that executor and should
own only cache location, mappings, and restoration:

```text
published OCI environment
  ├── exact source and lock provenance
  ├── sparse check capsule
  └── Lake input-to-output mappings
                 │
                 ▼
toolchain-scoped Lake artifact cache
                 │
                 ▼
ProjectExecutor ─────► ordinary Lake target graph
```

The cache directory must be keyed by the exact Lean toolchain identity and
platform ABI. It must never mix artifacts across incompatible toolchains.

## Experiment before implementation

Use one generated Mathlib 4.33 project and preserve the same source and shared
dependency graph for every measurement. Measure preparation, Lake graph work,
local compilation, and total wall time separately.

Compare:

1. the current bare Lake build through `lean-runtime build`;
2. explicit root-package, local-library, root-module `leanArts`, and executable
   targets;
3. the same targets with `LAKE_ARTIFACT_CACHE=true` and a toolchain-scoped
   `LAKE_CACHE_DIR`;
4. the cache with publication mappings captured by `lake build -o` and
   registered locally;
5. warm process-cache and cold filesystem-cache runs.

The experiment must use Lake commands from the resolved toolchain. It should
record the precise target syntax that preserves all declared root outputs while
avoiding unrelated dependency package defaults.

## Publication and acquisition

If the experiment succeeds, environment publication should run the ordinary
verified build with a Lake mappings output. Publication should bind the mapping
digest to the same exact source revision, complete Lake graph, toolchain, and
platform record as the capsule.

Acquisition should:

1. verify the environment and mapping descriptors through the existing OCI
   trust path;
2. install compatible artifacts into the toolchain-scoped Lake cache or add
   mappings that point to a configured remote service;
3. leave Lake responsible for evaluating input traces and restoring outputs;
4. fail explicitly on corrupt mappings or artifacts instead of silently
   treating them as verified environment content;
5. permit an ordinary source build when policy allows it.

OCI remains the environment and provenance distribution format. Lake's cache
format remains the build-cache contract. Translation between them belongs in a
small adapter, not in capsule, registry, or project CLI code.

## Performance gate

The slice ships only if the same empty Mathlib project satisfies:

| Journey | Budget |
| --- | ---: |
| Focused warm file check | at most 3 seconds |
| Project-wide check | at most 10 seconds |
| First build with shared cache ready | at most 10 seconds |
| Warm no-op build | at most 3 seconds |
| Executable build | comparable to native Lake |
| Custom facet | handled unchanged by Lake |

Correctness gates must include libraries, executables, multiple local modules,
custom Lean options, Mathlib plus LeanCert, a supported custom facet, an
unsupported cache entry, corrupted cache content, and concurrent projects using
the same cache.

If explicit targets and the native cache meet the budgets, no specialized
builder should be added. If Lake still spends most of a minute traversing
verified upstream artifacts, add project-wide `check` as the fast proof
workflow and pursue an opaque-prebuilt-dependency optimization in Lake. A
specialized local-module checker is a last resort and must be called `check`,
not `build`.
