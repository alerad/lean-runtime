# Compatibility profiles

Profiles exercise real dependency universes rather than mocking Lake. The Mathlib
4.32.2 and 4.33.0 profiles independently import Mathlib plus its eight
direct/transitive ecosystem packages.

```bash
python scripts/run_compatibility.py compatibility/mathlib-4.32.2.json
python scripts/run_compatibility.py compatibility/mathlib-4.33.0.json
```

The first run downloads and builds the environment. Later runs reuse its
content-addressed sources, artifacts, and published workspace.
