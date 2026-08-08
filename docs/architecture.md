# Architecture

## Ready-to-run programs

A ready-to-run program is the small result you can open immediately, without
rebuilding its Lean project first. Lean Runtime verifies its files every time it
is opened and records the exact source revision and, when known, the environment
that produced it. Use one for fast service execution. Open the full environment
when you need kernel replay, custom compilation, or an independent rebuild.

Program libraries and portable program copies use OCI-compatible storage under
the hood. That transport detail does not appear in the ordinary Python API or CLI.

## Dominant abstraction

Lean Runtime is an environment compiler:

```text
EnvironmentSpec
      │
      ▼
EnvironmentResolver ─────► EnvironmentLock
                                │
           immutable Git trees │
                                ▼
EnvironmentStore ───────► Published Environment
                                │
                       copy-on-write clone
                                ▼
Backend ────────────────► Execution Instance
                                │
                                ▼
                         ExecutionResult
                          + provenance
```

The specification and lock are platform-independent where possible. The
published environment identity includes platform and build-profile inputs.
Each execution attempt has a unique history identity. A separate stable request
digest includes the environment, input digest, operation, and requested policy.

## Public identities

Two lifecycle identities are central to the ordinary API:

- `environment_id`: exact lock plus platform/build inputs;
- `execution_id`: one concrete execution attempt.

`request_digest` is a stable comparison key for identical logical requests. It
does not name a mutable history record.

Inspection also exposes `spec_digest` and `lock_id` for auditing. They remain
lower-level identities rather than separate lifecycle objects users must name.

## Resolution

Resolution:

1. normalizes and installs the selected toolchain;
2. validates exact direct Git commits or explicit tags and unique package names;
3. generates a minimal Lake root;
4. invokes that toolchain's `lake update`;
5. retains the resulting manifest;
6. verifies every Git checkout against the manifest commit;
7. records each Git tree identity;
8. rejects declared package toolchains that disagree with the specification;
9. atomically publishes exact source snapshots and the canonical lock.

Tags exist only at the specification boundary. The lock and all subsequent
identities contain the resolved full commit and Git tree.

Packages may intentionally declare an earlier toolchain while remaining
compatible with the selected environment. Direct and transitive differences
emit compatibility events; the actual environment build remains authoritative
instead of treating a package's development pin as a version constraint.

Lean Runtime does not interpret semantic-version constraints or invent a
second dependency solver.

## Discovery

Discovery is an internal subsystem layered above the exact Runtime API:

```text
Lean source
    │
    ▼
static evidence + exact catalog
    │
    ▼
bounded candidate plan
    │
    ▼
Runtime.open_exact(candidate lock)
    │
    ▼
Lean acceptance or rejection
```

Catalog metadata and deterministic scores only choose which exact locks to
try. A candidate succeeds exclusively when Runtime materializes its verified
identity and Lean accepts the source. Discovery does not resolve transitive
dependencies, alter environment identity, infer minimum versions, or replace
Lake. Once a lock is found, callers can use the ordinary deterministic Runtime
path directly.

## Materialization

Materialization is guarded by an OS-level cross-process lock. A process builds
in a unique staging directory, optionally runs explicitly configured artifact
commands, invokes `lake build`, writes ready-state metadata, and atomically
renames the stage to its final identity.

Crashes cannot publish a partially ready environment. Concurrent processes
requesting the same identity converge on the same published directory.

## Execution

A published build is not used as an ordinary mutable working directory. Each
execution receives a disposable clone. macOS clonefiles and Linux reflinks are
used when available, with a normal copy fallback.

The source is written only into that instance. The selected backend executes
Lean or Lake there, collects bounded output, normalizes diagnostics, records
requested versus enforced policy, publishes an execution record, and deletes
the instance.

Kernel acceptance is expected to reproduce for the same locked environment and
source. Logs, paths, elapsed time, and arbitrary native build behavior are not
claimed to be byte-for-byte deterministic.

## Offline invariant

Opening a completed environment by name or digest reads only:

- published environment metadata;
- its retained lock;
- its built workspace.

No resolver or network operation runs. This is tested by deleting the original
Git repository and reopening/checking the environment from a second process.

## Aliases and garbage collection

Names are atomic JSON pointers to environment identities. Updating a name does
not mutate either old or new environments. Garbage collection removes only
old environments that are not reachable through an alias; locks and source
snapshots are conservatively retained by store schema 1.
Last-use records prevent recently opened or executed unnamed environments from
being collected, and execution cloning shares the environment's deletion lock.

## Trust boundary

The local backend is for trusted code. Exact pins protect reproducibility and
supply-chain identity, but packages can still execute code during Lake
configuration or builds. Network isolation is therefore not advertised by the
local backend. The `Backend` interface exists so container and remote workers
can enforce stronger policies later.
