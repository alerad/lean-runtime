# Lean Runtime

Lean Runtime runs Lean proofs from Python or a single `.lean` file. It discovers
local Lake projects or compiles friendly exact dependencies into an
exact reusable environment, then returns structured results and a record of what
was used.

```text
lean-runtime run FILE / lean-run FILE / lean.setup(CONTEXT)
                │
                ├── pinned local project ───────────> ProjectEnvironment
                └── dependencies / exact lock ─────> Environment
                                                        │
                                               check / build / execute
                                                        │
                                                        ▼
                                               ExecutionResult + provenance
```

- `lean-runtime run FILE` discovers or selects context for one file.
- `lean-runtime check` operates in a known environment or pinned project.
- `lean-run FILE` is a shortcut for `lean-runtime run FILE`.

It deliberately does not replace the official tools:

- **Elan** installs and selects Lean toolchains.
- **Lake** resolves packages and builds workspaces.
- **Lean Runtime** prepares exact environments, reuses downloaded and built
  files, runs Lean, and records what was used.

## Current scope

The front-facing API supports setup-once Python environments, one-shot helpers,
friendly exact package references, standalone TOML frontmatter, local-project
discovery, shared Lake dependencies, exact lock output, batch checking, and
asyncio. New projects can begin with a shared catalog-pinned Mathlib graph;
existing project trees can preview and transactionally adopt the same layout.
The explicit runtime also exposes environment libraries, portable copies,
trusted publishers, verification, captures, policies, and storage lifecycle
operations.

The local backend executes **trusted inputs only**. Lean packages and Lake
configuration can run native programs and arbitrary build commands; the local
backend is not a security sandbox.

[Get started](getting-started.md){ .md-button .md-button--primary }
[Read the architecture](architecture.md){ .md-button }
