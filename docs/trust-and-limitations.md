# Trust and limitations

## Trusted local execution

Lean Runtime 0.5 orchestrates trusted code. It is not a sandbox.

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

Local OCI bundle import verifies the complete blob digest chain, recomputes the
lock and environment identities, verifies package Git commits and trees, and
runs a Lean probe before publication. Registry pulls apply the same checks.
Ordinary availability failures may fall back to source in `auto` mode; integrity,
lock, archive-safety, and probe failures do not. A bundle still contains trusted
executable build output: these checks do not prove that its builder compiled the
locked sources faithfully. Registry authentication is not a builder signature.
Required Cosign policy authenticates an expected publisher workflow and index
digest, but still trusts that workflow to compile the locked sources faithfully.
Publishers can attach a signed source/probe/build-inventory attestation, and
`lean-runtime verify --rebuild` independently reacquires the locked sources,
rebuilds them, reruns the Lean probe, and compares artifact inventories.

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
