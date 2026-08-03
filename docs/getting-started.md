# Getting started

## Install

```bash
python -m pip install lean-runtime
```

On macOS and Linux, Lean Runtime downloads a checksum-verified, pinned Elan
installer into its private cache. It then installs requested Lean versions on
demand. It does not modify the user's default Elan toolchain or shell profile.

Windows users currently need to point `LEAN_RUNTIME_ELAN` at an existing Elan
executable.

## Create an environment

Dependencies use exact 40-character Git commits:

```python
from lean_runtime import EnvironmentSpec, GitPackage, Runtime

runtime = Runtime()
spec = EnvironmentSpec(
    toolchain="4.32.2",
    packages=(
        GitPackage(
            name="mathlib",
            url="https://github.com/leanprover-community/mathlib4.git",
            rev="905b95818eb32af7874a58b427f50c1711a5e96c",
            root_module="Mathlib",
            artifact_command=("lake", "exe", "cache", "get"),
        ),
    ),
)

lock = runtime.resolve(spec)
lock.write("environment.lock.json")
environment = runtime.ensure(lock, name="mathlib-4.32.2")
```

`resolve()` asks Lake for a concrete dependency graph. `ensure()` acquires the
exact locked sources, builds the environment once, and atomically publishes it.

## Check Lean

```python
result = environment.check(
    """
    import Mathlib

    example : 2 + 2 = 4 := by norm_num
    """
)

assert result.ok
print(result.environment_id)
print(result.provenance.request_digest)
```

Each run has a unique `execution_id`, so repeated checks do not overwrite
history. Identical logical requests share a stable `request_digest`.

## Reopen offline

After the environment has been published, it can be opened by alias or digest
without resolving dependencies or accessing the network:

```python
environment = Runtime().open("mathlib-4.32.2")
result = environment.check("import Mathlib\nexample : True := by trivial")
```
