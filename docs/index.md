# Lean Runtime

Lean Runtime runs Lean proofs from Python or a single `.lean` file. It discovers
local Lake projects or compiles friendly exact dependencies into a
exact reusable environment, then returns structured results and a record of what
was used.

```text
lean-run FILE / lean.setup(CONTEXT)
                │
                ├── pinned local project ───────────> ProjectEnvironment
                └── dependencies / exact lock ─────> Environment
                                                        │
                                               check / build / execute
                                                        │
                                                        ▼
                                               ExecutionResult + provenance
```

It deliberately does not replace the official tools:

- **Elan** installs and selects Lean toolchains.
- **Lake** resolves packages and builds workspaces.
- **Lean Runtime** prepares exact environments, reuses downloaded and built
  files, runs Lean, and records what was used.

## Current scope

The front-facing API supports setup-once Python environments, one-shot helpers,
friendly exact package references, standalone TOML frontmatter, local-project
discovery, exact lock output, batch checking, and asyncio. The explicit runtime
also exposes environment libraries, portable copies, trusted publishers,
verification, captures, policies, and storage lifecycle operations.

The local backend executes **trusted inputs only**. Lean packages and Lake
configuration can run native programs and arbitrary build commands; the local
backend is not a security sandbox.

[Get started](getting-started.md){ .md-button .md-button--primary }
[Read the architecture](architecture.md){ .md-button }
