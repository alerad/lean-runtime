# Proof-batch benchmark

Prepare an exact environment, then run the lifecycle harness from the repository root:

```bash
python scripts/run_v1_case_study.py research-stack --output results.json
```

Record cold preparation separately with `lean-runtime --timings ensure LOCK`. The harness
measures warm concurrent checks, deterministic export, import into an empty runtime home,
offline verification, and replay. Keep the raw JSON and record the machine, filesystem,
runtime version, lock, cache state, and command line with any published summary.
