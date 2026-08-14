# Compatibility profiles

Profiles exercise published environments rather than mocking Lake. The Mathlib
4.32.2 and 4.33.0 profiles independently acquire their bundled exact catalog
locks and import the public `Mathlib` root. Transitive packages remain attested
in the lock but are not advertised as independent catalog roots.

```bash
python scripts/run_compatibility.py compatibility/mathlib-4.32.2.json
python scripts/run_compatibility.py compatibility/mathlib-4.33.0.json
```

The first run downloads the verified sparse environment. Later runs reuse its
content-addressed artifacts and published workspace.
