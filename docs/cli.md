# Command line

Lean Runtime 4 exposes intentions, not its storage implementation.

## Daily commands

```text
lean-runtime new NAME
lean-runtime adopt [PATH]
lean-runtime check [PATH...]
lean-runtime watch FILE
lean-runtime build [TARGET...]
lean-runtime update
lean-runtime publish
```

All project commands use the current directory by default. `check` selects the
nearest pinned Lake project or discovers an exact standalone environment. It
accepts files, directories, stdin (`-`), or no path for the current project.

Use `--using CONTEXT` only to override inference. It accepts a project path,
lock path, environment name, Lean version, or package reference. Prefixes
`project:`, `lock:`, `env:`, `toolchain:`, and `package:` disambiguate names.

```bash
lean-runtime check Main.lean --using mathlib@v4.33.0
lean-runtime check Main.lean --using environment.lock.json --offline
lean-runtime check Main.lean --using research-stack
```

`watch` is the file-watching operation. Repeated performance samples remain a
variant of checking:

```bash
lean-runtime watch MyProject/Basic.lean
lean-runtime check MyProject/Basic.lean --repeat 5
lean-runtime check Main.lean --matrix compatibility.toml
```

## Guided mutation

`new`, `adopt`, `update`, `publish`, `doctor`, and `clean` print what they will
do and confirm in a terminal. `--yes` is the non-interactive spelling.
`--dry-run` requests an inspection-only adoption, update, share/unshare, or
cleanup.

## Persistent configuration

Machine policy belongs in `~/.config/lean-runtime/config.toml` (or the file
named by `LEAN_RUNTIME_CONFIG`). A nearest project `lean-runtime.toml` may
override it. Command-line values are temporary overrides.

```toml
[runtime]
home = "/var/lib/lean-runtime"
libraries = ["ghcr.io/example/lean-runtime-cache"]
availability = "auto" # auto, required, or local

[trust]
publisher_verification = "required" # required or ignore
trusted_publisher = "https://github.com/example/repo/.github/workflows/publish.yml@refs/heads/main"
trusted_issuer = "https://token.actions.githubusercontent.com"
verification_tool = "cosign"
```

The project file can keep its existing adoption metadata at the top level;
only the `[runtime]` and `[trust]` tables participate in CLI configuration.

## Inspection

```bash
lean-runtime status
lean-runtime status Main.lean
lean-runtime verify SUBJECT
lean-runtime doctor
lean-runtime clean
```

## Advanced namespaces

```text
env list
env info NAME
env lock SPEC
env acquire LOCK
env diff A B
env export NAME --output FILE
env import FILE
env publish / env finalize

project info / scan / share / unshare / lock / export
program create / run / info / export / import / acquire / publish / finalize
toolchain list / info / install / optimize / publish / finalize
storage usage / verify
catalog build
```

`replay` and `completion` remain top-level reproducibility utilities.

There are no compatibility aliases for the v3 surface. In particular, `run`,
`init`, `prepare`, `open`, `download`, `environments`, `inspect`, `compare`,
`copy`, `finalize`, `project attach/detach`, and `toolchain slim` are invalid.
The `lean-run` and `lean-runtime-catalog` executables are not installed.

## Exit status

- 0: successful operation or accepted proof
- 1: completed negative result, such as a rejected proof or blocked plan
- 2: invalid invocation, missing context, integrity, or infrastructure failure
- 3–5: classified publication failures
- 130: interrupted operation
