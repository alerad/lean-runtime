# Execution captures

An execution capture is a canonical JSON replay manifest. It contains the
complete environment lock, relative source tree, entrypoint, execution policy,
operation, and an optional expected result.

```python
capture = environment.capture(source, expected_ok=True)
capture = environment.capture_files(files, entrypoint="Main.lean", expected_ok=True)
capture.write("result.execution.json")

result = Runtime().replay_capture("result.execution.json")
```

Captures are manifests, not portable binary archives. They do not currently
embed Git repositories or compiled artifacts. A clean machine may need network
access to acquire exact locked commits before its first replay; a machine with
the environment already published can replay offline.

## Identity semantics

- `capture_id` identifies the canonical captured inputs.
- `request_digest` identifies a logical execution request.
- `execution_id` identifies one concrete attempt and is always unique.

This avoids losing timing and output history when an identical proof is checked
more than once, while retaining a stable key for comparison and deduplication.

Captures are not signed attestations. Treat captures and their embedded lock
files as trusted inputs in the local backend.
