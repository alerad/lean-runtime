# Environments

## Specification, lock, publication

Lean Runtime separates three states:

1. `EnvironmentSpec` is the human-authored intent.
2. `EnvironmentLock` is Lake's fully resolved graph in canonical JSON.
3. `Environment` is a platform-specific, built, published environment.

The separation lets one process resolve a lock and another materialize it. A
completed environment can subsequently be opened offline.

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
  provide a friendly input that resolution converts to an exact commit.
- `root_module` is imported by the synthetic root library, ensuring the
  dependency's Lean artifacts are built.
- `subdir` records a safe relative package subdirectory.
- `artifact_command` is an explicit trusted hydration command.

Artifact commands run from the **synthetic root workspace**, not from the
dependency checkout. This supports commands such as `lake exe cache get` in the
resolved environment. Commands beginning with `lake` or `lean` are routed
through the locked toolchain.

## Identity

The environment identity includes the complete lock, host platform, and the
implemented build profile. Version 0.3 supports only the `release` profile;
other values are rejected rather than producing misleadingly distinct IDs for
identical builds.

Friendly names are mutable aliases:

```text
research-stack -> env_74fbe13a...
```

Updating an alias never mutates or deletes the old environment.

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

```python
report = runtime.gc(dry_run=True)
report = runtime.gc(dry_run=False, minimum_age_seconds=30 * 24 * 60 * 60)
```
