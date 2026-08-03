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

Dependencies may use exact commits or friendly tags. Locks always contain exact
commits:

```python
from lean_runtime import EnvironmentSpec, GitPackage, Runtime

runtime = Runtime()
spec = EnvironmentSpec(
    toolchain="4.32.2",
    packages=(
        GitPackage.tag(
            name="mathlib",
            url="https://github.com/leanprover-community/mathlib4.git",
            tag="v4.32.2",
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

For generated projects, submit a complete relative source tree:

```python
result = environment.check_files(
    {
        "Support/Defs.lean": "def answer : Nat := 42",
        "Main.lean": "import Support.Defs\nexample : answer = 42 := by rfl",
    },
    entrypoint="Main.lean",
)
```

## Reopen offline

After the environment has been published, it can be opened by alias or digest
without resolving dependencies or accessing the network:

```python
environment = Runtime().open("mathlib-4.32.2")
result = environment.check("import Mathlib\nexample : True := by trivial")
```
