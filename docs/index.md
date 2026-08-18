---
hide:
  - navigation
  - toc
---

<div class="lr-hero" markdown>

<span class="lr-eyebrow">LEAN 4 ENVIRONMENTS, WITHOUT THE BUSYWORK</span>

# Check the proof. Reuse everything else.

Lean Runtime gives standalone files, Lake projects, and Python programs one
exact, cache-aware way to run Lean. It discovers the right context, reuses
compatible toolchains and dependencies, and records precisely what ran.

[Get started](getting-started.md){ .md-button .md-button--primary }
[Explore the CLI](cli.md){ .md-button }

```bash
python -m pip install lean-runtime
lean-runtime check Main.lean
```

</div>

<div class="lr-proof-line" markdown>

**One command** · **Exact dependency graphs** · **Warm and offline reuse** ·
**Structured provenance**

</div>

## Start from where you are

<div class="grid cards" markdown>

-   :material-file-code-outline:{ .lg .middle } **I have a Lean file**

    ---

    Let imports describe the environment. Lean Runtime discovers an exact
    compatible Mathlib release, acquires only the required capsule closure,
    and keeps diagnostics on your original filename.

    [`lean-runtime check Main.lean` →](standalone-files.md)

-   :material-folder-cog-outline:{ .lg .middle } **I have a Lake project**

    ---

    Work in the current directory. Keep the existing toolchain and manifest;
    optionally adopt shared dependency storage without changing revisions.

    [`lean-runtime check` →](local-projects.md)

-   :material-creation-outline:{ .lg .middle } **I want a new project**

    ---

    Create a catalog-pinned project with exact Lake metadata and a reusable
    dependency graph, then check it immediately.

    [`lean-runtime new MyProof` →](getting-started.md#new-project)

-   :material-language-python:{ .lg .middle } **I am calling Lean from Python**

    ---

    Prepare once, check many sources, run batches concurrently, and retain
    typed diagnostics, cancellation, identities, and provenance.

    [`lean.setup(...)` →](python-api.md)

</div>

## A small daily interface

```text
new NAME       create a project
adopt [PATH]   share exact dependencies from existing projects
check [PATH…]  check a project, directory, source file, or stdin
watch FILE     re-check on save
build [TARGET] build the current project
update         preview and apply a transactional dependency update
status [PATH]  explain what Lean Runtime selected
```

Project commands default to the current directory. `check` uses the nearest
pinned Lake project when one exists and performs bounded exact-environment
discovery otherwise. Advanced storage and publication machinery stays under
noun namespaces such as `env`, `project`, `program`, and `toolchain`.

[Command-line reference](cli.md){ .md-button }

## Exact when it matters

<div class="lr-pipeline" markdown>

```text
source or project
       │
       ▼
context discovery ──► exact lock ──► verified environment
                                            │
                                            ▼
                                      Lean execution
                                            │
                                            ▼
                         result + diagnostics + provenance
```

</div>

Lean Runtime treats identity and lifecycle as product features:

- package tags resolve to full Git commits and tree hashes;
- locks and environments have canonical content-derived identities;
- sparse capsules verify manifests, frame digests, and projected artifacts;
- identical requests share a request digest while each attempt gets a unique
  execution ID;
- acquisition, project sharing, and updates stage and probe before publication;
- compatible user Elan toolchains are reused read-only.

[How environments work](environments.md){ .md-button }
[Architecture](architecture.md){ .md-button }

!!! warning "Trusted execution, not a sandbox"

    Lean packages and Lake configuration can execute native programs and build
    commands. Lean Runtime verifies identities and artifacts, but its local
    backend is intended for trusted inputs. Read the
    [trust boundary](trust-and-limitations.md) before running third-party locks
    or packages.

## Go deeper

<div class="grid cards" markdown>

- **Portable environments** — export complete copies or acquire source-free
  sparse capsules from OCI libraries. [Read the guide →](portable-copies.md)
- **Verification and replay** — inspect exact identities, capture executions,
  compare environments, and replay requests. [Verification →](v1-precision.md)
- **Publishing** — generate a multi-platform GitHub workflow with isolated
  capsule probes, signatures, and clean-consumer checks.
  [Publishing guide →](project-publishing.md)
- **Python automation** — use synchronous, async, batch, multi-file, matrix,
  interactive, and cancellation APIs. [Python reference →](python-api.md)

</div>
