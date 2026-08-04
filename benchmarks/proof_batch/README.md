# Proof-batch benchmark

Prepare the exact Mathlib environment, then run the release harness from the repository root:

```bash
python scripts/run_v1_case_study.py research-stack \
  --concurrency 1 --concurrency 4 --concurrency 8 --concurrency 20 \
  --repeat 5 --output benchmarks/proof_batch/results.json
```

The committed candidate fixture intentionally mixes accepted proofs, elaboration failures, and
a syntax error. The harness records raw wall-time samples, summary statistics, machine/runtime
metadata, cache state, stable request versus unique execution identity, deterministic export,
import into an empty runtime home, offline verification, and replay.

Cold preparation must be measured separately because its cache state is part of the result:

```bash
lean-runtime --home /tmp/lean-runtime-cold --prebuilt never --timings \
  ensure environment.lock.json
```

Do not commit headline numbers without the raw JSON, exact lock, machine specification, command,
and stated cache state. Public CI timing is informational and must not be used as a hard gate.
