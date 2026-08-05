# V1 release case study

The release case study demonstrates the public lifecycle rather than relying on private
benchmark hooks. It uses a prepared exact Mathlib environment and the committed mixture of
accepted proofs, elaboration failures, and malformed input.

## Run the evidence harness

```bash
python scripts/run_v1_case_study.py research-stack \
  --concurrency 1 --concurrency 4 --concurrency 8 --concurrency 20 \
  --repeat 5 --output benchmarks/proof_batch/results.json
```

The resulting JSON records:

- runtime, Python, platform, command, and fixture identity;
- cache state before and after the workload;
- raw wall-time samples and min/median/mean/p95/max by concurrency;
- stable request identity and unique execution identity;
- bundle size and export/import durations;
- import into an empty runtime home;
- offline verification and captured-execution replay.

The harness deliberately starts from an already prepared environment. Measure cold source
preparation separately and state whether local, Lake, and OCI libraries were empty. Do not combine
cold source builds and warm execution into one headline number.

## Clean-wheel acceptance

Build and exercise the artifact users actually install:

```bash
python -m build
python scripts/smoke_wheel.py dist/lean_runtime-1.0.0-py3-none-any.whl
```

Add `--lean` to bootstrap an isolated runtime home and run a real standalone proof. The smoke
script creates a fresh virtual environment, installs only the wheel, clears ambient Python
package visibility, checks both console entry points, and reads a packaged v1 schema through
`lean_runtime.schema_path()`.

## Publishing results

Keep the raw JSON beside any summary. Record the exact lock and environment ID, machine and
filesystem, command line, runtime version, repetition count, and cache state. Timings from noisy
shared CI are evidence that the workflow completes, not a performance regression gate.
