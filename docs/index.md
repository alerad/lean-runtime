---
hide:
  - navigation
  - toc
---

<div class="lr-intro" markdown>

<div class="lr-intro-copy" markdown>

# Check the proof. Skip the setup.

Write a Lean file. Run one command. Lean Runtime picks a compatible
Mathlib release, downloads only what your imports need, and checks your
proof — no toolchain install, no Lake project, no cache scripts.

[Get started in two minutes](getting-started.md){ .lr-primary-link }
[CLI reference](cli.md)

</div>

<div class="lr-demo" markdown>

**An empty directory. One file.**

```lean
-- Main.lean
import Mathlib
example : 2 + 2 = 4 := by norm_num
```

```console
$ lean-runtime check Main.lean
Discovering an exact environment
✓ Main.lean accepted in 1.60s
```

<p class="lr-demo-note">That is the entire setup. The second run reuses everything and works offline.</p>

</div>

</div>

## Start where you are

<div class="grid cards" markdown>

-   :material-file-code-outline:{ .lg .middle } **I have a Lean file**

    ---

    Your imports already say what you need. Lean Runtime finds a matching
    Mathlib release, fetches only the pieces your file uses, and reports
    errors on your own filename.

    [`lean-runtime check Main.lean` →](standalone-files.md)

-   :material-folder-cog-outline:{ .lg .middle } **I have a Lake project**

    ---

    Works in place. Your toolchain and manifest stay exactly as they are;
    sharing dependency storage across projects is optional and reversible.

    [`lean-runtime check` →](local-projects.md)

-   :material-creation-outline:{ .lg .middle } **I'm starting fresh**

    ---

    One command creates a project pinned to a known-good Mathlib and
    toolchain pair, ready to check immediately.

    [`lean-runtime new MyProof` →](getting-started.md)

-   :material-language-python:{ .lg .middle } **I'm calling Lean from Python**

    ---

    Set up once, then check one proof or thousands — concurrently, with
    typed results, timeouts, and cancellation.

    [`lean.setup(...)` →](python-api.md)

</div>

## Seven commands cover a normal day

```text
new NAME       create a project
adopt [PATH]   share dependency storage with existing projects
check [PATH…]  check a project, directory, file, or stdin
watch FILE     re-check on save
build [TARGET] build the current project
update         preview and apply a dependency update
status [PATH]  explain what Lean Runtime selected and why
```

Everything defaults to the current directory. The heavier machinery
(publishing, exports, storage) lives under namespaces like `env` and
`program`, out of your way until you want it.

[Full CLI reference](cli.md){ .md-button }

## What happens when you run `check`

1. **It reads your file.** The imports tell it which packages you need.
2. **It picks an exact environment.** Not "latest Mathlib" — one specific
   release, pinned down to the commit, so the result is repeatable.
3. **It fetches only what's needed.** Verified pieces land in a shared
   cache; your next file or project reuses them instead of re-downloading.
4. **It runs Lean.** You get ordinary diagnostics on your own filename,
   plus a record of exactly what ran — enough to reproduce the same check
   tomorrow or on another machine.

Want the exactness story in full — locks, identities, verification?

[Verify and compare](v1-precision.md){ .md-button }
[Architecture](architecture.md){ .md-button }

!!! warning "Trusted code only"

    Lean packages can run code while they build. Lean Runtime verifies
    everything it downloads, but it is not a sandbox — only check sources
    and dependencies you trust. [Read the trust boundary](trust-and-limitations.md).

## Go deeper

<div class="grid cards" markdown>

- **Take it anywhere** — export a built environment as one file and open it
  on another machine, no rebuild. [Portable environments →](portable-copies.md)
- **Prove it ran** — verify identities, diff environments, capture and
  replay executions, measure timings. [Verify and compare →](v1-precision.md)
- **Publish your project** — one command generates a multi-platform GitHub
  workflow with signing and clean-machine checks. [Publishing →](project-publishing.md)
- **Automate with Python** — sync, async, batch, matrix, and interactive
  APIs with the same exactness guarantees. [Python API →](python-api.md)

</div>
