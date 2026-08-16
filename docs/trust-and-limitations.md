# Trust and limitations

## Trusted local execution

Lean Runtime orchestrates trusted code. It is not a sandbox.

Lake configurations are executable, dependencies may contain native code and
custom build targets, and explicit artifact commands run subprocesses. Lockfile
schema validation prevents malformed paths and command values, but validation
does not make an untrusted lock safe to execute.

This also applies to `Environment.execute()` and `spawn_interactive()`: command
targets and protocol inputs must be trusted. Interactive sessions enforce the
same local resource controls as one-shot execution, but they do not turn local
Lake tools into isolated untrusted services.

Do not submit untrusted packages, locks, captures, Lean source, or artifact
commands to the local backend. A future container backend can provide a stronger
boundary without changing the environment model.

## Supply chain

The automatic Elan bootstrap uses a pinned release installer and verifies its
SHA-256 digest before execution. Lean toolchains and exact Git package revisions
are then acquired through Elan and Git. This is integrity hardening, not a full
signed software-supply-chain system.

Legacy OCI bundle import verifies the complete blob digest chain, recomputes
the lock and environment identities, verifies package Git commits and trees,
and runs a Lean probe before publication. Sparse capsules verify the platform
index digest chain, normalized module graph, pack ranges, per-frame digests, and
every projected artifact digest, then run Lean against the source-free
projection. Published slim toolchains verify their index, exact platform
ABI and Lean commit, archive limits, manifest, and a six-part capability corpus
before becoming visible. Registry pulls apply the corresponding checks.
Ordinary availability failures may fall back to source in `auto` mode; integrity,
lock, archive-safety, and probe failures do not. A bundle still contains trusted
executable build output: these checks do not prove that its builder compiled the
locked sources faithfully. Registry authentication is not a builder signature.
Required Cosign policy authenticates the expected publisher workflow and both
capsule and toolchain index digests, but still trusts that workflow to compile
the locked sources faithfully.
Publishers can attach a signed verification-report attestation covering
source and probe results (a normalized build-inventory comparison is
produced by `verify --rebuild`, not embedded in that attestation), and
`lean-runtime verify --rebuild` independently reacquires the locked sources,
rebuilds them, reruns the Lean probe, and compares artifact inventories.

A check capsule is evidence that the trusted publisher accepted the recorded
statements and build artifacts; removing source and build inputs does not make
those artifacts independently derivable. Native compilation, `native_decide`,
editor source navigation, and development builds are outside the capsule's
batch-check capability and require the exact full environment and toolchain.

## Reproducibility boundary

The same lock identifies the same source graph, and an environment identity also
includes platform/build inputs. Kernel acceptance should reproduce for the same
inputs. Logs, elapsed time, filesystem paths, native builds, and arbitrary
package scripts are not promised to be byte-for-byte deterministic.

## Deliberately deferred

- semantic version solving, floating branches, and editable dependencies;
- native in-process signature and attestation verification;
- untrusted sandboxed execution;
- remote workers;
- automatic package conflict explanations.

Output capture is bounded and exposes `output_truncated`; the retained output
does not include a synthetic truncation marker. Diagnostic parsing is
best-effort and original process output remains available.
