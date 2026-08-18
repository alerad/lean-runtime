# Execution captures

A capture is a JSON file that records everything needed to re-run a check
and compare the result: the exact environment lock, the source tree, the
entrypoint, the execution policy and operation, and optionally the expected
outcome. Useful for regression suites and "does this still check?" audits.

```python
capture = environment.capture(source, expected_ok=True)
capture = environment.capture_files(files, entrypoint="Main.lean", expected_ok=True)
capture.write("result.execution.json")

result = Runtime().replay_capture("result.execution.json")
```

Captures are manifests, not portable binary archives. They do not currently
embed Git repositories or compiled artifacts. A clean machine may need network
access to acquire exact locked commits before its first replay; a machine with
the environment already published can replay offline. For a sparse capsule
environment, offline replay additionally requires the capture's imported
modules to be projected locally already.

## Identity semantics

- `capture_id` identifies the canonical captured inputs.
- `request_digest` identifies a logical execution request.
- `execution_id` identifies one concrete attempt and is always unique.

This avoids losing timing and output history when an identical proof is checked
more than once, while retaining a stable key for comparison and deduplication.

Captures are not signed attestations. Treat captures and their embedded lock
files as trusted inputs in the local backend.
