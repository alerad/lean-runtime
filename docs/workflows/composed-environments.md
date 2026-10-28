# Named multi-repository environments (prerelease)

Use a composition when you want a new project to import libraries from several
repositories without copying or rebuilding their common dependencies for every
consumer. A composition is **one exact toolchain and compatible package graph**,
not a global search path over every registered repository.

## Preview → capture → warm → create → build

Use actual repository paths and exported Lean module names:

```sh
lean-runtime --availability local env compose /path/A /path/B --name research
```

Review exact source/dependency pins, source-byte estimates and blockers. Required
local path dependencies must be included. Conflicting toolchains, revisions,
package names or modules are blockers, never implicit upgrades. Then capture:

```sh
lean-runtime --availability local env compose /path/A /path/B --name research --yes
lean-runtime --availability local env warm research --import A.Basic --import B.Basic
lean-runtime --availability local new ./MyResearch --env research
```

Review the project preview before creating it:

```sh
lean-runtime --availability local new ./MyResearch --env research --yes
lean-runtime --availability local build ./MyResearch
lean-runtime --availability local check ./MyResearch/Main.lean
```

Original repositories are read-only inputs. Capture honors Git ignores, omits
`.git` and generated `.lake` content, and rejects unsupported symlinks. The new
project is an ordinary Lake project with generated shared dependency links and
`lean-runtime.environment.json`. Never edit `.lake/packages` or the packages
reached through those links. Use Runtime builds/checks to serialize shared writes.

`--availability local` prevents implicit acquisition. If an exact compiler or
external dependency is missing, explicitly prepare it before warming/building.
Do not run `lake update` merely to make incompatible repositories compose.

## Pins and build cost

`research` is a mutable alias; each created project pins an immutable `composed_`
ID. Reusing the alias for a new composition does **not** repin existing projects.
Changes to original repositories also require a new capture. Compare identities:

```sh
lean-runtime env composed-list
lean-runtime env composed-info research
lean-runtime env composed-diff OLD_COMPOSED_ID research
```

Prefer warming specific `--import` modules. `--all-libraries` builds every
selected local library; `--all-targets` also builds package defaults/executables.
Shared compatible outputs are reused, but the first build can still be expensive.
Dynamic Lake configurations use conservative invalidation. Readiness records
prove a past build, not that externally removed caches remain present.

## Budgets, retention and cleanup

Capture defaults to 200,000,000 source bytes and 20,000 files. Override using
`env compose --max-source-bytes BYTES --max-files COUNT`; use repeatable
`--exclude PATTERN` for extra source exclusions.

Global `--max-build-bytes` and `--max-dependency-bytes` flags go **before** `env`
or the build command. They monitor owned growth, not total free disk capacity;
monitoring is cooperative, not an OS quota. Existing shared caches are not new
source copies. Inspect before deleting:

```sh
lean-runtime env composed-refs
lean-runtime env composed-clean
```

Aliases and registered consumer references retain exact environments. Cleanup
previews unreferenced owned compositions and package views; apply only after
review with `--yes`. `composed-refs --drop-alias NAME` and `--drop-project KEY`
also preview unless `--yes` is supplied. Dropping a live project's reference can
make its environment collectible; do not use it as routine cache cleanup.

## Portability and restored projects

```sh
lean-runtime env composed-export research --output research.zip
lean-runtime env composed-import research.zip --name imported-research
```

Review the import preview and repeat with `--yes`. The verified ZIP contains
source snapshots and exact pins, **not toolchains or compiled artifacts**. A new
machine must obtain the exact external dependencies separately. For a restored
pinned project, `env composed-attach ./MyResearch` previews restoring its generated
links; apply with `--yes` only after review.

These snapshot commands are separate from the older Git-only `env list`, `info`,
`lock`, `acquire`, `export` and `import` commands. Their lock formats are not
interchangeable.

## Anvil equivalent

Anvil uses **adopted pool IDs/names**, not arbitrary host paths. Its donor registry
and adopted pool are distinct; registration does not establish compatibility.

```sh
anvil lean pool list '{}'
anvil lean env plan --projects '["goldenmeanings","fifispentagonal"]'
```

After reviewing, use the returned `planDigest` with the identical selection:

```sh
anvil lean env compose --projects '["goldenmeanings","fifispentagonal"]' --name research --plan-digest REVIEWED_DIGEST
anvil lean env warm research --imports '["Fifispentagonal.Basic"]'
anvil lean env new research --name MyResearch
anvil lean env new research --name MyResearch --apply
anvil lean env build WORKSPACE_ID
anvil lean env check research --source 'example : (1 : Nat) + 1 = 2 := by decide'
```

Replace `WORKSPACE_ID` with the ID returned by `new --apply`; edit through Anvil
workspaces. Native `new` creates a host directory; Anvil `new` creates a durable
workspace. Builds use the workspace root, not a source snapshot or subdirectory.
Every flag also has a canonical typed JSON equivalent:

```sh
anvil lean env warm '{"environment":"research","imports":["Fifispentagonal.Basic"],"timeoutSeconds":900,"maxBuildBytes":128000000}'
```

Anvil build/check are sandboxed, local-only and network-denied. Exact external
Git dependencies must already be in the managed cache. Default pool limits are
128 MB per composition (maximum 200 MB), 200 MB aggregate sources
(adopted plus composed), 512 MB aggregate cache, and 128 MB
per-build growth, monitored cooperatively. `--compiler-timeout` controls compiler
execution; `--timeout` is the outer RPC deadline. `--check` only validates the
request shape; **it does not run Lean**. A normal Lean rejection is a successful
RPC containing `verdict: rejected`; infrastructure failures have no verdict.

Inspect `anvil lean env --help` and leaf `--help` for field descriptions, and
`anvil modules inspect lean.cli` for the walkthrough. Source capsules and Runtime
retention/cleanup remain native CLI operations. Anvil world capsules alone do
not preserve the shared host Runtime cache; durable pool recovery currently uses
a raw-state fallback.
