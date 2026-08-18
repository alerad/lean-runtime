# Getting started

Install the released wheel:

```bash
python -m pip install lean-runtime
lean-runtime --version
```

For a new project:

```bash
lean-runtime new MyProof
cd MyProof
lean-runtime check
```

For an existing pinned Lake project:

```bash
cd ExistingProject
lean-runtime check
lean-runtime adopt
```

The initial check works before adoption. Adoption is the optional storage and
reuse optimization: it verifies the current manifest and dependency checkouts,
reuses their bytes, previews the transition, and rolls back if the attached
graph does not probe successfully.

For a standalone proof:

```bash
lean-runtime check Main.lean
```

Lean Runtime infers the context. Add strict source frontmatter or `--using`
only when you need an exact override. A second check reuses the exact cached
environment; `--offline` guarantees that no acquisition occurs.

Run `lean-runtime status` to see what was selected and `lean-runtime doctor`
when a prerequisite or store needs attention.
