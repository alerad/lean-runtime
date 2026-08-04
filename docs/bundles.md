# Environment bundles

Lean Runtime can move an already built environment between compatible machines
without rebuilding its Lake packages:

```bash
lean-runtime export research-stack --output research-stack.oci.tar.gz
lean-runtime --home /tmp/fresh import research-stack.oci.tar.gz --name research-stack
```

The equivalent Python API is:

```python
info = runtime.export_environment("research-stack", "research-stack.oci.tar.gz")
environment = another_runtime.import_environment(
    "research-stack.oci.tar.gz", name="research-stack"
)
```

Import verifies the OCI manifest and every blob digest, recomputes the lock and
environment identities, requires an exact platform compatibility match, checks
each package's Git commit and tree, and runs a Lean probe. Publication uses a
staging directory and atomic rename, so a failed import is never visible as a
ready environment. `--no-probe` exists for inspection and testing workflows;
normal imports should keep the probe enabled.

## Format version 1

The gzip file is a deterministic OCI image-layout archive. It contains a
standard `oci-layout`, `index.json`, one image manifest, a Lean Runtime config
blob, and content-addressed layer blobs. The config contains the complete
`EnvironmentLock`, build profile, environment identity, and platform
compatibility record.

There is one layer for the synthetic root workspace and one layer for each Lake
package. Package layers include both source and `.lake/build`, so identical
serialized package trees can be reused by a future registry transport. The Lean
toolchain is not bundled; Elan supplies the exact toolchain named by the lock.

Archives use sorted paths, zero timestamps and ownership, normalized tar
metadata, canonical JSON, and deterministic gzip headers. Exporting an unchanged
environment twice therefore produces identical bytes and digests.

## Compatibility and trust

`lock_id` identifies the platform-independent locked source graph.
`environment_id` additionally includes the release build profile and the
versioned platform compatibility record. Informational host details remain in
metadata but do not invalidate compatible caches across OS patch releases.

A bundle is trusted executable build output. Digest, lock, Git-tree, and probe
verification detect corruption and identity substitution, but do not prove that
the builder compiled the sources faithfully. Phase 1 intentionally provides no
signature or remote-registry policy; only import bundles obtained from a trusted
source.
