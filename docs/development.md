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

## Release focus

The decisive invariant is:

> One process can resolve and build environment X; another can open X by digest
> without resolution or network access; every execution names X and retains its
> own history record.

Apache-2.0 covers the Python package and repository source.
