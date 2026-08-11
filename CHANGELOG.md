# Changelog

## Unreleased

## 2.0.8 - 2026-08-10

- Add immutable, content-addressed provenance to ready-to-run program
  descriptions and accept it through `program-create --provenance-file`.
- Preserve the identity and import compatibility of existing version 1 program
  descriptions while emitting version 2 descriptions for newly created programs.

## 2.0.7

- Retry an OCI layer once from a clean partial after size or SHA-256 integrity
  verification fails, bypassing intermediary caches while preserving strict
  digest validation and emitting structured retry diagnostics.

## 2.0.6

- Preserve the highest-ranked Lean compiler rejection when automatic discovery
  exhausts its candidates, including raw output and parsed diagnostics in both
  human and JSON `lean-run` output.
- Add line-oriented and NDJSON request/response helpers to `InteractiveSession`
  for incremental REPLs and ready-to-run checker programs.

## 2.0.5

- Integrate bounded exact-environment discovery into the `lean-runtime` package.
- Make `lean-run FILE` discover standalone Mathlib environments automatically when
  no exact context or pinned Lake project is present.
- Bundle an initial deterministic catalog for core Lean v4.32.2 and Mathlib
  v4.32.2, v4.31.0, and v4.30.0, including exact Runtime locks and module inventories.
- Add `lean_runtime.discovery` for explicit planning, catalog, and authoritative
  discovery APIs, plus the `lean-runtime-catalog` maintainer command.

Version 2.0.4 was not published because its release tag retained 2.0.3 package
metadata.

## 2.0.3

- Accept portable-copy symlinks whose normalized targets remain inside the
  extracted copy, including common links such as `docs/README.md -> ../README.md`.
- Continue rejecting absolute or escaping symlink targets, duplicate archive
  members, and later archive writes that traverse an extracted symlink.

## 2.0.2

- Remove staged environments safely when Git-for-Windows leaves source pack
  files read-only or briefly locked.
- Preserve the primary environment-build diagnostic when staging cleanup also
  fails.
- Show the failed phase, command, exit code, and captured tool output when a
  verbose CLI materialization fails.

## 2.0.1

- Enable Git's long-path support for every Runtime-managed source operation so
  Mathlib environments can be prepared and materialized on Windows.
- Preserve the primary Git diagnostic when cleanup of a failed Windows source
  checkout also encounters locked files.

## 2.0.0

Version 2 gives the public interface the language used by Lean users rather
than the language of its storage implementation. The environment format stays
compatible; command names, Python names, configuration, events, and public
metadata intentionally change without aliases.

### Public terminology

- Environment libraries replace OCI caches in ordinary configuration and docs.
- Downloadable environments replace prebuilt artifacts.
- Portable copies replace OCI bundles.
- Publisher verification replaces signature-policy terminology.
- Cleanup and storage replace garbage-collection and blob terminology.
- Ready-to-run programs replace execution-capsule and container terminology.

### Main migrations

- `Runtime(caches=..., prebuilt=...)` becomes
  `Runtime(libraries=..., availability=...)`.
- `resolve`, `ensure`, and named `open` become `prepare`, `open_exact`, and
  `environment` in the explicit Python API.
- `export_environment` and `import_environment` become `save_portable_copy`
  and `open_portable_copy`.
- CLI workflows use `prepare`, `open`, `download`, `build-and-publish`,
  `save-copy`, `open-copy`, `compare`, `storage`, and `clean`.
- Environment libraries accept friendly `ghcr.io/owner/name` locations; the
  OCI transport remains an advanced implementation detail.
- Ready-to-run programs can be created, verified, copied, downloaded from a
  program library, published for multiple kinds of computers, and interrupted
  during interactive execution.

## 1.0.0

Lean Runtime v1 establishes the concise `lean-run` and `lean.setup()` workflows while making
exact environments independently verifiable, explainable, measurable, and portable.

### Breaking changes

- Remove `lean-runtime audit`, `Runtime.audit()`, `AuditReport`, and `ArtifactInventory`;
  `verify` is the sole trust surface and `verify --rebuild` performs independent rebuild checks.
- Close and version the seven public CLI JSON schemas. Execution payloads now include stable
  phase timings, while `inspect` and `gc` use one canonical data shape each.
- Remove transitional compatibility surfaces introduced before v1; callers should use the
  top-level façade, `Runtime`, and the documented v1 result types directly.

### Release capabilities

- Add lazy `setup`, `check`, `check_file`, and `replay` Python façade functions.
- Add `lean-run` with strict TOML frontmatter, automatic local-project discovery,
  exact lock input/output, concise progress, and structured JSON output.
- Add exact `mathlib@REVISION`, `leancert@REVISION`, and
  `owner/repository@REVISION` package references without permitting floating aliases.
- Add `ExecutionResult.raise_for_error()` and structured `LeanCheckError` failures.
- Rewrite the README around the front-facing workflow and expand the standalone
  file, CLI, Python, and routing documentation.
- Discover pinned local Lake projects from contained Lean files and expose a
  distinct mutable `ProjectEnvironment` API.
- Preserve project-relative file checks and record content, configuration,
  manifest, and Git project provenance without claiming an environment identity.
- Add transparent OCI prebuilt-cache lookup with authenticated registry pulls,
  disk-backed blob reuse, strict fallback policy, and explicit `pull` support.
- Add deterministic OCI publishing through `build-and-push`, with blobs and the
  platform manifest committed before the lock-level index tag.
- Stream bundle layers and OCI archives through temporary files instead of
  materializing multi-gigabyte package layers in memory.
- Add deterministic OCI image-layout export and verified, atomic environment import.
- Verify bundle digests, lock and environment identities, package Git trees,
  archive paths, host compatibility, and a Lean probe before publication.
- Separate artifact compatibility identity from informational host metadata and
  bump the environment store identity schema.
- Add verification, decision explanations, semantic context diffs, repeated profiles, and
  bounded matrix execution over ordinary execution results.
- Add interruptible toolchain installation, Lake resolution, environment builds, matrix checks,
  and project checks with process-group cleanup on cancellation and Ctrl-C.
- Ship the public JSON schemas in wheel and source distributions and expose `schema_path()`.
- Add reproducible case-study fixtures, clean-wheel smoke testing, and installed-wheel Lean
  acceptance in CI.

## 0.6.0

- Discover packages declared with either `lakefile.toml` or `lakefile.lean`.
- Translate Lake DSL configurations through the package's exact declared Lean
  toolchain instead of parsing Lean source or guessing package metadata.

## 0.5.0

- Add `Environment.execute()` for generic commands such as `lake exe`.
- Add managed `InteractiveSession` processes with live UTF-8 standard-I/O pipes.
- Run interactive tools in disposable environment clones with exact provenance.
- Enforce local timeout, memory, CPU, and bounded-transcript policies for sessions.
- Gracefully close sessions with stdin EOF before process-group termination.
- Persist final interactive `ExecutionResult` records and clean up instances.

## 0.4.0

- Add `github:owner/repository@tag-or-commit` package references.
- Discover package identity, root module, and Lean toolchain from Lake projects.
- Pin convenience references to exact commits before environment resolution.
- Add one-shot `lean-runtime check FILE --with REFERENCE` execution.
- Add `Runtime.spec_from_references()`, `resolve_references()`,
  `ensure_references()`, and `Runtime.check(..., packages=[...])`.
- Detect incompatible discovered toolchains across multi-package requests.
- Avoid Elan's implicit toolchain installation during installation checks.

## 0.3.0

- Add safe multi-file checking and replayable multi-file captures.
- Add cancellable native asyncio helpers.
- Add structured lifecycle progress events.
- Resolve friendly Git tags into exact commit locks.
- Add installation health, cache status, environment listing, and richer inspection.
- Add a scheduled real-ecosystem compatibility profile.
- Store compact, content-verified one-commit source snapshots.
- Treat direct and transitive package toolchain files as compatibility signals,
  while making the actual selected-toolchain build authoritative.
- Use execution leases so batch checks can clone concurrently without racing GC.
- Build imported locked-package roots on demand inside execution workspaces.
- Add release automation for PyPI trusted publishing.
- Build and deploy the documentation site through GitHub Pages.

## 0.2.0

- Introduce content-addressed environments, exact Git locks, offline reopening,
  execution provenance, captures, aliases, garbage collection, and trusted local
  resource policies.
