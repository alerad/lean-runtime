# Environments

## Specification, lock, publication

Lean Runtime separates three states:

1. `EnvironmentSpec` is the human-authored intent.
2. `EnvironmentLock` is Lake's fully resolved graph in canonical JSON.
3. `Environment` is a platform-specific, built, published environment.

The separation lets one process resolve a lock and another materialize it. A
completed environment can subsequently be opened offline; for a sparse
capsule, checking is offline for already projected import closures, while a
new import extends the projection from a configured library unless
`availability="local"` refuses it.

Downloadable environments are sparse check capsules. A capsule stores a
normalized module graph and content digest for each Lean artifact, resolves to
the same exact lock and platform environment identity as a locally built
environment, and its local projection grows as new imports are checked without
changing that identity. Complete source-bearing environments are still
produced locally by source builds and exchanged as portable copies.

Package references are an input compiler for `EnvironmentSpec`, not another
environment type:

```python
spec = runtime.spec_from_references(["github:alerad/leancert@v4.32.2.4"])
```

The reference checkout supplies the package name, first importable library
root, and declared toolchain. Its tag is pinned to a commit before the
specification enters Lake resolution.

## Package fields

```python
GitPackage(
    name="sample",
    url="https://github.com/example/sample.git",
    rev="0123456789abcdef0123456789abcdef01234567",
    root_module="Sample",
    subdir=None,
    artifact_command=(),
)
```

- `name` is the Lake package identity.
- `url` is a Git remote.
- `rev` is a full commit hash; `GitPackage.tag(...)` and TOML `tag = "..."`
  provide a friendly input that resolution converts to an exact commit. The
  resolved lock always contains the exact commit and tree, while the
  requested tag may remain alongside it as requested-revision metadata (and,
  for manually authored tagged specifications, in the recorded specification
  digest).
- `root_module` is imported by the synthetic root library, ensuring the
  dependency's Lean artifacts are built.
- `subdir` records a safe relative package subdirectory.
- `artifact_command` is an explicit trusted hydration command.

Artifact commands run from the **synthetic root workspace**, not from the
dependency checkout. This supports commands such as `lake exe cache get` in the
resolved environment. Commands beginning with `lake` or `lean` are routed
through the locked toolchain.

## Identity

The environment identity includes the complete lock, a versioned platform
compatibility record, and the implemented build profile. Informational details
such as the Python platform and OS patch release are retained in metadata but
are not identity inputs. Full environments and portable imports record that
informational platform record; sparse registry downloads currently persist
only the versioned compatibility record. The runtime currently supports only the `release` profile;
other values are rejected rather than producing misleadingly distinct IDs for
identical builds.

Friendly names are mutable aliases:

```text
research-stack -> env_74fbe13a...
```

Updating an alias never mutates or deletes the old environment.

Store schema 2 changed this compatibility identity. Environments produced by
schema 1 are intentionally cache misses and must be rebuilt or re-imported;
aliases to their old IDs do not migrate automatically.

## Storage and garbage collection

The default cache is `~/Library/Caches/lean-runtime` on macOS and
`~/.cache/lean-runtime` on Linux. Override it with `LEAN_RUNTIME_HOME` or the
`Runtime(home=...)` argument.

Source snapshots use shallow, one-commit Git repositories rather than copying a
resolver checkout's complete history. A content digest detects modifications to
the checked-out files. This keeps large dependency universes practical while
preserving the exact commit and Git tree identity required by Lake.

Garbage collection only considers unnamed environments and uses last-opened or
last-executed usage records, rather than directory modification time alone.
Short-lived execution leases prevent deletion during cloning without
serializing concurrent checks. Locks and source snapshots remain retained in
the current store schema.

Disposable execution and dependency-resolution workspaces carry their own
process-held ownership leases. `storage` includes them under `Scratch`, and
`clean` previews and reclaims abandoned workspaces while retaining every
workspace with a live owner. Legacy scratch directories created before ownership
metadata are removed only by an explicit `clean --execute` after the safety grace.

Sparse downloads are stored once by artifact digest under the shared module
CAS and hardlinked into environment projections when the filesystem permits.
`lean-runtime storage` reports this category separately. Because it is shared,
its logical byte count can overlap environment projections and should not be
added to their logical sizes as an estimate of physical disk use.
`clean --include-downloads` reclaims old OCI blobs and unleased CAS artifacts;
per-artifact locks and recency updates make collection during an active
projection unlikely, though projection does not yet hold a strict lease
across unpack-and-project.

```python
report = runtime.clean(dry_run=True)
report = runtime.clean(dry_run=False, minimum_age_seconds=30 * 24 * 60 * 60)
```
