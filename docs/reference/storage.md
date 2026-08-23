# Storage

Lean Runtime retains exact toolchains, sources, environments, and compiled
artifacts so later checks can run without repeating acquisition or builds.

## Inspect usage

```console
lean-runtime storage usage
```

The report distinguishes two totals:

- **Materialized** sums the logical size of every retained category. A file
  hard-linked from the shared CAS into an environment appears in both category
  rows.
- **Allocated** counts hard-linked files once and uses allocated filesystem
  blocks. It remains an estimate because filesystems such as APFS can share
  copy-on-write blocks without exposing that relationship through ordinary file
  metadata.

Consequently, materialized size is useful for understanding retained content
but is not a claim about unique physical disk consumption.

`lean-runtime storage verify` rebuilds the storage ledger by scanning the whole
store. It can take significant time on a large cache; ordinary usage reads the
cached ledger when its fingerprint is current.

## Scratch workspaces

Checks, builds, dependency resolution, and interactive programs use disposable
workspaces. Normal completion removes them. A workspace is considered abandoned
when it is old enough and no process holds its ownership lease.

Legacy workspaces from releases that predate ownership markers are retained for
at least 24 hours before `doctor --yes` removes them.

## Reclaim space

Preview cleanup first:

```console
lean-runtime clean --dry-run
```

Apply the displayed plan explicitly:

```console
lean-runtime clean --yes
```

Named, recent, and in-use environments are retained. Add `--all` only when the
download cache should also be considered. Shared project packages remain
available for project reuse.
