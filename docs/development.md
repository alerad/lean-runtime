# Development

```bash
git clone git@github.com:alerad/lean-runtime.git
cd lean-runtime
python -m pip install -e '.[dev,docs]'
ruff check .
ruff format --check .
mypy lean_runtime
pytest
mkdocs build --strict
```

The default Pytest configuration excludes the real Lean integration test. Run
it explicitly with:

```bash
pytest -m integration -vv
```

That test creates an exact local Git package, resolves it with Lake,
materializes the lock concurrently from two processes, checks Lean, removes the
original repository, replays a capture offline, and opens the environment from
a second process.

The scheduled ecosystem suite resolves Mathlib and independently checks its
major transitive libraries:

```bash
python scripts/run_compatibility.py compatibility/mathlib-4.32.2.json
```

## Release focus

The decisive invariant is:

> One process can resolve and build environment X; another can open X by digest
> without resolution or network access; every execution names X and retains its
> own history record.

Apache-2.0 covers the Python package and repository source.

## Publishing to PyPI

Releases use PyPI trusted publishing; no long-lived API token is stored in
GitHub. Before the first release, create a **pending publisher** from the
Publishing page of the maintainer's PyPI account with:

- PyPI project name: `lean-runtime`
- GitHub owner: `alerad`
- GitHub repository: `lean-runtime`
- Workflow: `release.yml`
- Environment: `pypi`

In the GitHub repository, create the `pypi` environment under **Settings →
Environments**. An approval rule for that environment is recommended.

To publish, update the version and changelog, merge a green CI revision, and
create a GitHub release whose tag is exactly `v<version>`—for example,
`v0.4.0`. The release workflow verifies that the tag matches
`project.version`, builds both distributions, and publishes through OpenID
Connect. The first successful publication converts the pending publisher into
a normal publisher and creates the PyPI project.

PyPI does not permit replacing files for an already published version. If a
release needs a correction, increment the version rather than recreating the
tag.
