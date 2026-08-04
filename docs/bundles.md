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
environment = another_runtime.import_environment("research-stack.oci.tar.gz", name="research-stack")
```

Import verifies the OCI manifest and every blob digest, recomputes the lock and
environment identities, requires an exact platform compatibility match, checks
each package's Git commit and tree, and runs a Lean probe. Publication uses a
staging directory and atomic rename, so a failed import is never visible as a
ready environment. `--no-probe` exists for inspection and testing workflows;
normal imports should keep the probe enabled.

Layer construction, archive writing, import, and registry downloads are
disk-backed and streamed. Peak memory does not scale with package-layer size.

## OCI global caches

By default, Lean Runtime checks the public
`oci://ghcr.io/alerad/lean-runtime-cache` mirror. A miss or availability failure
falls back to the existing source build, so environment specifications do not
change. Set `LEAN_RUNTIME_CACHES=` or construct `Runtime(caches=[])` to disable
all remote cache lookups.

Configure one or more cache repositories without changing the environment
specification or lock:

```python
runtime = Runtime(
    caches=["oci://ghcr.io/alerad/leancert-runtime"],
    prebuilt="auto",
)
environment = runtime.ensure(lock)
```

The equivalent environment variables are:

```bash
export LEAN_RUNTIME_CACHES=oci://ghcr.io/alerad/leancert-runtime
export LEAN_RUNTIME_PREBUILT=auto
```

`auto` tries caches in order and builds from source when an artifact is absent,
incompatible, or temporarily unavailable. `require` makes an ordinary cache
miss an error. `never` disables remote lookup. Digest, lock, archive-safety, and
probe failures are security failures and never silently fall back to source.

Explicit prefetch uses the same verified path:

```bash
lean-runtime \
  --cache oci://ghcr.io/alerad/leancert-runtime \
  pull environment.lock.json
```

Registry blobs are retained content-addressed under the runtime home. Pulling a
second environment with an identical package layer reuses it without another
download.

Old blobs can be included in garbage collection explicitly:

```bash
# Preview, then apply after reviewing the candidates.
lean-runtime gc --include-blobs
lean-runtime gc --include-blobs --execute
```

Blobs referenced by an imported environment or leased by an active pull are
retained. Collection rechecks both conditions while holding the same per-blob
lock used by downloads.

### Required publisher signatures

For high-trust workflows, require a Sigstore signature from one exact GitHub
Actions identity:

```python
runtime = Runtime(
    caches=["oci://ghcr.io/alerad/leancert-runtime"],
    signatures="require",
    trusted_identity=(
        "https://github.com/alerad/leancert/.github/workflows/cache.yml@refs/heads/main"
    ),
    trusted_issuer="https://token.actions.githubusercontent.com",
)
```

CLI equivalents are `--signatures require`, `--trusted-identity`, and
`--trusted-issuer`. Verification uses an installed Cosign 2.6.2 or 3.0.4+ and
binds the canonical lock-index digest, certificate identity, issuer, and
transparency-log claims. Older versions are rejected because of the patched
[Cosign verification advisory](https://github.com/sigstore/cosign/security/advisories/GHSA-whqx-f9j3-ch6m).

Signature failure is an integrity failure and never triggers source fallback.

## Publishing

Publish the current platform after ensuring the lock:

```bash
export LEAN_RUNTIME_REGISTRY_USERNAME=alerad
export LEAN_RUNTIME_REGISTRY_PASSWORD="$GHCR_TOKEN"

lean-runtime build-and-push environment.lock.json \
  --push-to oci://ghcr.io/alerad/leancert-runtime \
  --tag v4.32.2.4 \
  --sign --attest
```

The publisher checks for existing blobs, uploads only missing content, publishes
the platform manifest by digest, and updates the canonical `lock_<sha>` index
tag last. Human tags are aliases to that same index. This follows the
[OCI Distribution Specification](https://github.com/opencontainers/distribution-spec/blob/main/spec.md)
and uses an [OCI image index](https://github.com/opencontainers/image-spec/blob/main/image-index.md)
for platform selection.

Repository authors can use the bundled composite action after checking out the
repository and generating or retaining a lock:

```yaml
permissions:
  contents: read
  packages: write

steps:
  - uses: actions/checkout@v4
  - uses: ./.github/actions/cache
    with:
      lock: environment.lock.json
      repository: oci://ghcr.io/${{ github.repository_owner }}/leancert-runtime
      tag: ${{ github.ref_name }}
      registry-username: ${{ github.actor }}
      registry-password: ${{ secrets.GITHUB_TOKEN }}
```

For a build matrix, have each platform publish without changing the canonical
index and retain its JSON result:

```bash
lean-runtime build-and-push environment.lock.json \
  --push-to oci://ghcr.io/alerad/leancert-runtime \
  --platform-only > platform-result.json
```

After collecting the result files, one final job publishes the deterministic
multi-platform index and human aliases:

```bash
lean-runtime publish-index "$LOCK_ID" results/*.json \
  --repository oci://ghcr.io/alerad/leancert-runtime \
  --tag "$GITHUB_REF_NAME"
```

The finalizer rejects duplicate OS/architecture/ABI entries. Publishing the
index only after every required platform succeeds prevents partial build
matrices from replacing a complete cache release.

`--attest` runs package-source verification and the Lean import probe, records a
stable inventory of all Lake build outputs, and publishes that predicate as a
keyless Cosign attestation bound to the platform manifest (or finalized index).
It requires Cosign and an OIDC-capable publishing environment.

## Auditing

Verify the embedded source markers, Git commits and trees, root lock material,
Lean probe, and build-output inventory at any time:

```bash
lean-runtime verify research-stack
```

For an independent check, reacquire and rebuild the exact lock in a temporary
store and compare normalized artifact inventories:

```bash
lean-runtime verify research-stack --rebuild
```

`source_verified` and `probe_passed` are the trust result. `artifact_match` is a
separate byte-reproducibility measurement: a mismatch is reported but is not
treated as failed source/proof verification, because native toolchains and package build
steps are not promised to produce byte-identical artifacts.

## Format version 1

The gzip file is a deterministic OCI image-layout archive. It contains a
standard `oci-layout`, `index.json`, one image manifest, a Lean Runtime config
blob, and content-addressed layer blobs. The config contains the complete
`EnvironmentLock`, build profile, environment identity, and platform
compatibility record.

There is one layer for the synthetic root workspace and one layer for each Lake
package. Package layers include both source and `.lake/build`, so identical
serialized package trees are reused by the registry transport. The Lean
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
the builder compiled the sources faithfully. Registry credentials authenticate
access but are not a builder attestation; only use cache publishers you trust.
