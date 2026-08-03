# Lean Runtime

Lean Runtime is a Python-native execution substrate for Lean 4. It compiles a
declarative dependency specification into a content-addressed environment, then
runs Lean with structured results and exact provenance.

```text
EnvironmentSpec ──resolve──> EnvironmentLock ──ensure──> Environment
                                                           │
                                                     check / build
                                                           │
                                                           ▼
                                                  ExecutionResult
                                                   + provenance
```

It deliberately does not replace the official tools:

- **Elan** installs and selects Lean toolchains.
- **Lake** resolves packages and builds workspaces.
- **Lean Runtime** owns acquisition, immutable identities, caching, execution
  policy, Python ergonomics, and provenance.

## Current scope

Version 0.2 supports exact Git dependencies, retained Lake manifests, immutable
published environments, mutable aliases, offline reopening, disposable
execution workspaces, structured diagnostics, resource policies, cancellation,
batch checking, and replayable JSON captures.

The local backend executes **trusted inputs only**. Lean packages and Lake
configuration can run native programs and arbitrary build commands; the local
backend is not a security sandbox.

[Get started](getting-started.md){ .md-button .md-button--primary }
[Read the architecture](architecture.md){ .md-button }
