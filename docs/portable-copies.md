# Portable environments

Built an environment once? You can move it to another compatible machine
as a single file and open it there — no rebuild, no re-download of Lake
packages:

```bash
lean-runtime env export research-stack --output research-stack.lean-environment
LEAN_RUNTIME_HOME=/tmp/fresh lean-runtime env import research-stack.lean-environment --name research-stack
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

`copy save` deliberately writes the complete environment, including sources,
for a self-contained development handoff that `verify` can check against the
locked Git trees. `project export` instead writes a source-free sparse check
capsule containing only the selected public module's transitive closure.
`copy open` detects and verifies both formats. Native compilation and project
development require the complete format; proof checking should prefer the
capsule.

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
    libraries=["ghcr.io/alerad/lean-runtime-cache"],
    availability="auto",
)
environment = runtime.open_exact(lock)
```

The equivalent environment variables are:

```bash
export LEAN_RUNTIME_LIBRARIES=ghcr.io/alerad/lean-runtime-cache
export LEAN_RUNTIME_AVAILABILITY=auto
```

`auto` tries libraries in order and builds locally when a copy is absent,
incompatible, or temporarily unavailable. `required` makes a missing copy an
error. `local` disables library lookup. Verification failures never silently
fall back to a local build.

Explicit prefetch uses the same verified path:

```bash
lean-runtime \
  --library ghcr.io/alerad/lean-runtime-cache \
  download environment.lock.json
```

Downloaded pack frames are verified into a raw module CAS under the runtime
home. Opening another environment with identical module bytes reuses them even
when its lock or top-level package differs.

Old blobs can be included in garbage collection explicitly:

```bash
# Preview, then apply after reviewing the candidates.
lean-runtime clean --all --dry-run
lean-runtime clean --all --yes
```

OCI blobs referenced by ready environments are retained. Sparse CAS
artifacts may be reclaimed because ready projections hold their own hardlink or
copy; sparse acquisition holds a collection lease across unpacking and
projection, and per-artifact locks and recency updates cover publication and
reuse, so a concurrent `clean --all` cannot reclaim an artifact
that is being projected.

### Required publisher verification

For high-trust workflows, require a Sigstore signature from one exact GitHub
Actions identity:

```python
runtime = Runtime(
    libraries=["ghcr.io/alerad/lean-runtime-cache"],
    publisher_verification="required",
    trusted_publisher=(
        "https://github.com/alerad/lean-runtime/.github/workflows/public-cache.yml@refs/heads/main"
    ),
    trusted_issuer="https://token.actions.githubusercontent.com",
)
```

CLI equivalents are `--publisher-verification required`, `--trusted-publisher`, and
`--trusted-issuer`. Verification uses an installed Cosign 2.6.2 or 3.0.4+ and
binds the canonical lock-index digest, certificate identity, issuer, and
transparency-log claims. Older versions are rejected because of the patched
[Cosign verification advisory](https://github.com/sigstore/verification_tool/security/advisories/GHSA-whqx-f9j3-ch6m).

Signature failure is an integrity failure and never triggers source fallback.

## Publishing

Check push access without building or uploading an environment:

```bash
lean-runtime env publish \
  --to ghcr.io/OWNER/lean-environments \
  --check-access
```

Add `--json` for the versioned `lean-runtime.publication/v1` success or failure
envelope. The process exit code retains the classification below.

For GHCR, credentials are selected in this order: the
`LEAN_RUNTIME_REGISTRY_USERNAME` / `LEAN_RUNTIME_REGISTRY_PASSWORD` pair, an
authenticated GitHub CLI session, then anonymous access. The access report names
the selected account and source but never prints the token. Lean Runtime does not
implicitly read Docker's credential store. To give the GitHub CLI package access,
run:

```bash
gh auth refresh -s write:packages,read:packages
```

Credential discovery is fail-closed: a configured provider that times out or
returns an unusable token stops publication before any registry request. The same
verified credential is retained from OCI preflight through upload; it is not
rediscovered after a long environment build. `--sign` and `--attest` run
Cosign as a separate process that authenticates with the explicit
`LEAN_RUNTIME_REGISTRY_USERNAME` / `LEAN_RUNTIME_REGISTRY_PASSWORD` pair or
its own ambient registry credentials, not with a retained GitHub CLI
credential. An explicitly supplied publication `--timeout` also applies to
credential discovery and registry network operations.

Publish the current platform after ensuring the lock:

```bash
export LEAN_RUNTIME_REGISTRY_USERNAME=alerad
export LEAN_RUNTIME_REGISTRY_PASSWORD="$GHCR_TOKEN"

lean-runtime env publish environment.lock.json \
  --to ghcr.io/OWNER/lean-environments \
  --tag v4.32.2.4 \
  --sign --attest
```

The same push-access probe runs automatically before the environment is built,
so a missing scope fails in seconds instead of after export. The probe starts
and immediately cancels an empty OCI upload session; it publishes no manifest or
index. The publisher then checks for existing blobs, uploads only missing
content, publishes the platform manifest by digest, and updates the canonical
index tag last, always as `capsule-lock_<sha>`. Environment libraries publish
and serve check capsules only. Every manifest is fetched back and its digest
is verified before success is reported. Human tags are aliases to the same
index.
This follows the
[OCI Distribution Specification](https://github.com/opencontainers/distribution-spec/blob/main/spec.md)
and uses an [OCI image index](https://github.com/opencontainers/image-spec/blob/main/image-index.md)
for platform selection.

Publication has stable failure codes for CI:

| Exit | Meaning | Retry? |
|---:|---|---|
| `2` | Invalid invocation or local input | Fix the invocation |
| `3` | Registry authentication or permission denial | Fix credentials/scopes first |
| `4` | Retryable network, throttling, or registry 5xx failure | Yes, with backoff |
| `5` | Partial or indeterminate publication state | Inspect the terminal state, then retry safely |

An unpublished failure explicitly states that no manifest or index was
finalized; already uploaded content-addressed blobs are unreferenced and safe to
reuse on retry. Once a manifest write has been attempted, any failure that
cannot prove the resulting remote state is reported as partial/indeterminate;
it is never reported as success. Registries do not use status codes
uniformly: in particular, GHCR can return 403 for missing permissions as well as
an inaccessible or not-yet-created package namespace, so diagnostics describe
these as likely causes rather than certainties. Automation can consume the same
state from the terminal `library.publish_failed` or `library.published` runtime
event.

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
      library: ghcr.io/${{ github.repository_owner }}/lean-environments
      tag: ${{ github.ref_name }}
      username: ${{ github.actor }}
      password: ${{ secrets.GITHUB_TOKEN }}
```

For a build matrix, have each platform publish without changing the canonical
index and retain its JSON result:

```bash
lean-runtime env publish environment.lock.json \
  --to ghcr.io/OWNER/lean-environments \
  --platform-only > platform-result.json
```

After collecting the result files, one final job publishes the deterministic
multi-platform index and human aliases:

```bash
lean-runtime env finalize "$LOCK_ID" results/*.json \
  --library ghcr.io/OWNER/lean-environments \
  --tag "$GITHUB_REF_NAME"
```

The finalizer rejects duplicate OS/architecture/ABI entries. Publishing the
index only after every required platform succeeds prevents partial build
matrices from replacing a complete cache release.

`--attest` runs the ordinary environment verification (package-source or
capsule-artifact checks plus the Lean import probe), records a stable
inventory of the Lake build outputs, and publishes both as a keyless Cosign
attestation of type `https://lean-runtime.dev/attestation/environment/v1`,
bound to the platform manifest (or finalized index). The predicate is the
versioned `lean-runtime.attestation/v1` document described by
`schemas/attestation-v1.schema.json`: it carries the verification report plus
a `build_inventory` of digest, entry count, and byte count, computed without
the independent rebuild that `verify --rebuild` performs. Attestation
requires Cosign and an OIDC-capable publishing environment.

## Auditing

Verify the embedded source markers, Git commits and trees, root lock material,
and Lean probe at any time:

```bash
lean-runtime verify research-stack
```

For an independent check, reacquire and rebuild the exact lock in a temporary
store and compare normalized artifact inventories:

```bash
lean-runtime verify research-stack --rebuild
```

The report's named checks are the trust result: `package_trees_verified` for
full bundles or `capsule_artifacts_verified` for sparse capsules, plus
`lean_probe_passed`. `artifact_match` is a separate byte-reproducibility
measurement produced by `--rebuild`: it compares normalized artifact
inventory digests (the inventory itself is not exported), and a mismatch is
reported but is not treated as failed source/proof verification, because
native toolchains and package build steps are not promised to produce
byte-identical artifacts.

## Advanced storage details

A complete portable copy is a deterministic OCI image-layout archive. It contains a
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

A sparse capsule uses the same outer OCI layout but replaces source/package
tarballs with seekable zstd packs and a normalized module manifest. Pack frames
carry their own compressed digest and an inventory of raw artifact digests, so
a ranged response is verified without downloading the surrounding pack. The
matching check-only Lean toolchain is a separate OCI index and is never hidden
inside the environment lock.

## Compatibility and trust

`lock_id` identifies the platform-independent locked source graph.
`environment_id` additionally includes the release build profile and the
versioned platform compatibility record. Informational host details remain in
metadata but do not invalidate compatible libraries across OS patch releases.

A portable copy is trusted executable build output. Digest, lock, Git-tree, and probe
verification detect corruption and identity substitution, but do not prove that
the builder compiled the sources faithfully. Library credentials authenticate
access but are not a builder attestation; only use environment publishers you trust.
