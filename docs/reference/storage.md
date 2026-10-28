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

## Compact duplicate build outputs

Shared packages deliberately retain separate compatibility keys for different
resolved graphs. In particular, dynamic `lakefile.lean` code can depend on the
whole graph: a stored dependency manifest is not sufficient evidence to merge
these build identities. This can retain many byte-identical compiled files.

Use copy-on-write compaction instead of deleting package trees or hard-linking
writable outputs:

```console
lean-runtime storage deduplicate --dry-run
lean-runtime storage deduplicate --yes
```

The default is a preview. Application requires `--yes`. Compaction covers managed
shared-project and composition build outputs of at least 64 KiB. Busy packages,
symlinks, and files with multiple hard links are skipped. It verifies a cloned
snapshot's SHA-256 before atomically replacing each duplicate, retaining target
permissions, timestamps, extended attributes and paths. Files remain separate
inodes: later writes to one package cannot change another. Build compatibility
keys, sources, traces, and toolchain installations are not removed or weakened.
As with builds, locks coordinate Runtime participants, not arbitrary external
writers; do not concurrently write managed cache files outside Runtime.

APFS and Linux filesystems supporting reflinks can share the underlying blocks.
Unsupported filesystems are left unchanged; there is no hard-link fallback.
`duplicate_bytes` and `cloned_bytes` describe identical content, **not guaranteed
reclaimed bytes**: APFS already-shared extents and filesystem snapshots can keep
blocks alive. Logical sizes and ordinary `du` output need not shrink. Compare
volume free space before/after, allowing for unrelated concurrent disk activity.

Shared builds automatically compact their outputs after execution, using a
disposable `deduplication.sqlite3` index under the runtime home to avoid hashing
unchanged files again. Index entries are hints, never authority to substitute
unverified content. Delete the index when no compactor is running to rebuild it.
Set `LEAN_RUNTIME_DEDUP=0` to disable automatic compaction. A failed automatic
optimization is reported without discarding outputs or failing the Lean build.
Artifact imports also use CoW copies where available, with independent ordinary
copies as the portable fallback.
