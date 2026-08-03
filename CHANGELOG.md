# Changelog

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
