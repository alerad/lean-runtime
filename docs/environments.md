# Exact environments

Normal users let `check` infer environments. Infrastructure authors use `env`:

```bash
lean-runtime env lock environment.toml --output environment.lock.json
lean-runtime env acquire environment.lock.json --name research-stack
lean-runtime env list
lean-runtime env info research-stack
lean-runtime env diff old.lock.json environment.lock.json
```

`env lock` resolves tags to complete Git commits and trees and computes the
canonical lock identity. `env acquire` makes that identity ready using verified
local content, configured libraries, or exact source materialization. The
mechanism does not change the semantic environment identity.

Use `env acquire --download-only` to require a published artifact with no
source fallback. Sparse environments can extend their verified import
projection without changing identity; local/offline policy refuses missing
closures before network access.

Portable complete environments use:

```bash
lean-runtime env export research-stack --output stack.lean-environment
lean-runtime env import stack.lean-environment --name imported-stack
```

Publication operators use `env publish` for one platform and `env finalize`
after collecting the required platform descriptors. These are deliberately not
top-level daily commands.
