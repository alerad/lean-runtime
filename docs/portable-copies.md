# Portable copies and environment libraries

Lean Runtime can move an already built environment between compatible machines
without rebuilding its Lake packages:

```bash
lean-runtime save-copy research-stack --output research-stack.lean-environment
lean-runtime --home /tmp/fresh open-copy research-stack.lean-environment --name research-stack
```

The equivalent Python API is:

```python
info = runtime.save_portable_copy("research-stack", "research-stack.lean-environment")
environment = another_runtime.open_portable_copy(
    "research-stack.lean-environment", name="research-stack"
)
```

Opening a copy verifies its exact environment identity, computer compatibility,
package revisions, and a real Lean import before making it available. A failed
open never appears as a ready environment. `--no-probe` exists for inspection
and testing; normal use should keep the probe enabled.

Saving, opening, and downloading are disk-backed and streamed. Peak memory does
not scale with the size of the environment.

## Environment libraries

By default, Lean Runtime checks the public
`ghcr.io/alerad/lean-runtime-cache` library. A missing or unavailable copy
falls back to the existing source build, so environment specifications do not
change. Set `LEAN_RUNTIME_LIBRARIES=` or construct `Runtime(libraries=[])` to disable
all library lookups.

Configure one or more libraries without changing the environment
specification or lock:

```python
runtime = Runtime(
    libraries=["ghcr.io/alerad/leancert-runtime"],
    availability="auto",
)
environment = runtime.open_exact(lock)
```

The equivalent environment variables are:

```bash
export LEAN_RUNTIME_LIBRARIES=ghcr.io/alerad/leancert-runtime
export LEAN_RUNTIME_AVAILABILITY=auto
```

`auto` tries libraries in order and builds locally when a copy is absent,
incompatible, or temporarily unavailable. `required` makes a missing copy an
error. `local` disables library lookup. Verification failures never silently
fall back to a local build.

Explicit prefetch uses the same verified path:

```bash
lean-runtime \
  --library ghcr.io/alerad/leancert-runtime \
  download environment.lock.json
```

Downloaded files are retained under the runtime home. Opening another
environment with identical dependencies reuses them without another download.

Old blobs can be included in garbage collection explicitly:

```bash
# Preview, then apply after reviewing the candidates.
lean-runtime clean --include-downloads
lean-runtime clean --include-downloads --execute
```

Files used by a ready environment or an active download are retained. Cleanup
rechecks both conditions before removing anything.

### Required publisher publisher_verification

For high-trust workflows, require a Sigstore signature from one exact GitHub
Actions identity:

```python
runtime = Runtime(
    libraries=["ghcr.io/alerad/leancert-runtime"],
    publisher_verification="required",
    trusted_publisher=(
        "https://github.com/alerad/leancert/.github/workflows/cache.yml@refs/heads/main"
    ),
    trusted_issuer="https://token.actions.githubusercontent.com",
)
```

CLI equivalents are `--publisher_verification required`, `--trusted-publisher`, and
`--trusted-issuer`. Verification uses an installed Cosign 2.6.2 or 3.0.4+ and
binds the canonical lock-index digest, certificate identity, issuer, and
transparency-log claims. Older versions are rejected because of the patched
[Cosign verification advisory](https://github.com/sigstore/verification_tool/security/advisories/GHSA-whqx-f9j3-ch6m).

Signature failure is an integrity failure and never triggers source fallback.

## Publishing

Publish the current platform after ensuring the lock:

```bash
export LEAN_RUNTIME_REGISTRY_USERNAME=alerad
export LEAN_RUNTIME_REGISTRY_PASSWORD="$GHCR_TOKEN"

lean-runtime build-and-publish environment.lock.json \
  --publish-to ghcr.io/alerad/leancert-runtime \
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
  - uses: ./.github/actions/publish-environment
    with:
      lock: environment.lock.json
      library: ghcr.io/${{ github.repository_owner }}/leancert-runtime
      tag: ${{ github.ref_name }}
      username: ${{ github.actor }}
      password: ${{ secrets.GITHUB_TOKEN }}
```

For a build matrix, have each platform publish without changing the canonical
index and retain its JSON result:

```bash
lean-runtime build-and-publish environment.lock.json \
  --publish-to ghcr.io/alerad/leancert-runtime \
  --platform-only > platform-result.json
```

After collecting the result files, one final job publishes the deterministic
multi-platform index and human aliases:

```bash
lean-runtime finalize-publication "$LOCK_ID" results/*.json \
  --library ghcr.io/alerad/leancert-runtime \
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

## Advanced storage details

The gzip file is a deterministic OCI image-layout archive. It contains a
standard `oci-layout`, `index.json`, one image manifest, a Lean Runtime config
blob, and content-addressed layer blobs. The config contains the complete
`EnvironmentLock`, build profile, environment identity, and platform
compatibility record.

There is one layer for the synthetic root workspace and one layer for each Lake
package. Package layers include both source and `.lake/build`, so identical
serialized package trees are reused by the registry transport. The Lean
toolchain is not bundled; Elan supplies the exact toolchain named by the lock.

Package verification binds every tracked file to the locked Git commit and
tree and rejects non-ignored untracked source. Git-ignored files generated by
package build tooling are treated as derived artifacts: their bytes remain
covered by the OCI layer digest and publisher trust policy, but they do not
change the locked source identity.

Archives use sorted paths, zero timestamps and ownership, normalized tar
metadata, canonical JSON, and deterministic gzip headers. Exporting an unchanged
environment twice therefore produces identical bytes and digests.

## Compatibility and trust

`lock_id` identifies the platform-independent locked source graph.
`environment_id` additionally includes the release build profile and the
versioned platform compatibility record. Informational host details remain in
metadata but do not invalidate compatible libraries across OS patch releases.

A portable copy is trusted executable build output. Digest, lock, Git-tree, and probe
verification detect corruption and identity substitution, but do not prove that
the builder compiled the sources faithfully. Library credentials authenticate
access but are not a builder attestation; only use environment publishers you trust.
