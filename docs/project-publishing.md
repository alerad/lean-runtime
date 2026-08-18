# Publish a project

Publishing lets other people (and your CI) check against your project
without building it — they download a verified, prebuilt copy instead.

From a clean, pushed, GitHub-backed project, it's one command:

```bash
lean-runtime publish
```

Lean Runtime inspects the current project, selects its sole library root,
derives a GHCR destination from `origin`, shows the publication plan, and
creates the maintained multi-platform GitHub workflow after confirmation.

The workflow freezes the source once, builds Linux AMD64, macOS AMD64, and
macOS ARM64 artifacts, differentially verifies source-free capsules, publishes
slim runtimes, signs indexes, finalizes only after the full matrix, and runs
anonymous clean-consumer checks. A failed/incomplete matrix cannot replace the
previous complete canonical index.

Advanced local primitives remain available:

```bash
lean-runtime project info
lean-runtime project lock --output project.lock.json
lean-runtime project export --output MyProject.lean-environment
lean-runtime env publish project.lock.json --to ghcr.io/owner/project-lean --platform-only
lean-runtime env finalize LOCK_ID linux.json macos-amd64.json macos-arm64.json \
  --library ghcr.io/owner/project-lean --sign
```

Publication rejects dirty or unpushed checkouts, missing/unsafe origins,
projects below the Git root, missing Lake/toolchain metadata, ambiguous library
roots, and failed isolated capsule probes.
