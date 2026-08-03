# Compatibility profiles

Profiles exercise real dependency universes rather than mocking Lake. The first
profile pins Mathlib 4.32.2 and independently imports Mathlib plus its eight
direct/transitive ecosystem packages.

```bash
python scripts/run_compatibility.py compatibility/mathlib-4.32.2.json
```

The first run downloads and builds the environment. Later runs reuse its
content-addressed sources, artifacts, and published workspace.
