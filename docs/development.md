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

The bundled discovery catalog is generated deterministically from exact locks:

```bash
lean-runtime catalog build catalog/environments.toml \
  --runtime-home .catalog-runtime \
  --output lean_runtime/discovery/data/catalog.json
```

Missing locks are resolved once; existing locks remain authoritative. Module
inventories come from Runtime-validated exact source snapshots. The builder
sets Mathlib's `MATHLIB_NO_CACHE_ON_UPDATE=1` by default because catalog
generation does not require the multi-gigabyte compiled cache.

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
`v0.5.0`. The release workflow verifies that the tag matches
`project.version`, builds both distributions, and publishes through OpenID
Connect. The first successful publication converts the pending publisher into
a normal publisher and creates the PyPI project.

PyPI does not permit replacing files for an already published version. If a
release needs a correction, increment the version rather than recreating the
tag.

## Release and announcement protocol

Catalog entries are public claims that a prebuilt environment is
downloadable. The protocol below makes those claims fail-closed:

1. Publish downloadable environments for every catalog entry and platform
   (`public-cache.yml`) **before** shipping a wheel that references them, and
   make the GHCR package publicly pullable.
2. Run `python scripts/registry_preflight.py` — it verifies, anonymously,
   that every catalog lock resolves to an index, platform manifests, and
   readable blobs. CI-cheap; run it on every release.
3. After publishing to PyPI, dispatch the **Announcement gate** workflow
   (`announcement-gate.yml`). It installs from PyPI on clean unauthenticated
   Linux and macOS runners and runs the advertised journeys from
   `acceptance/`: a cold check with `--no-source-build` (fail-closed: a
   broken registry fails in seconds instead of hiding behind a source
   build), a warm re-check, a failing proof with logical diagnostic paths, a
   lock round-trip, and the Python batch API. Release titles use `vX.Y.Z`,
   matching the tag.
4. Announce only after the gate is green. The same workflow runs daily as a
   canary: registry permissions and docs deployments can break without any
   code change.
