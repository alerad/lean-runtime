"""Normalized check capsules and exact module-closure planning.

Capsules are the physical, check-only representation of a built Lake
environment.  The manifest is independent of Lake's version-specific setup
JSON dialect and records every retained artifact by content digest.  Source
trees and development/editor facets are not part of the base check profile.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .errors import EnvironmentError
from .lockfiles import EnvironmentLock
from .serialization import canonical_json_bytes, write_json_atomic

CAPSULE_SCHEMA = "lean-runtime-check-capsule/1"
CAPSULE_MANIFEST = ".lean-runtime/capsule.json"
IMPORT_PARSER_SOURCE = (
    "import Lean.Elab.ParseImportsFast\n"
    "def main (args : List String) : IO Unit := Lean.printImportsJson args.toArray\n"
)

ArtifactCapability = Literal["check", "native", "editor", "development", "metadata"]
_LEAN_VERSION = re.compile(r"(?:leanprover/lean4:)?v?(\d+)\.(\d+)(?:\.(\d+))?")
_SOURCE_IMPORT = re.compile(r"^\s*(?:(?:public|meta)\s+)*import\s+(.+?)\s*$", re.MULTILINE)


def source_import_roots(source: str) -> tuple[str, ...]:
    """Return unique top-level module roots from ordinary Lean import headers."""
    return tuple(
        dict.fromkeys(
            module for match in _SOURCE_IMPORT.finditer(source) for module in match.group(1).split()
        )
    )


def _digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def artifact_capability(path: Path | PurePosixPath) -> ArtifactCapability:
    """Classify one build artifact into a runtime capability."""
    name = path.name
    if name.endswith(".ilean"):
        return "editor"
    if name.endswith((".olean", ".olean.private", ".olean.server", ".ir", ".ir.sig")):
        return "check"
    if name.endswith((".so", ".dylib", ".dll", ".export")):
        return "native"
    if name.endswith((".c", ".bc", ".o", ".a")):
        return "development"
    return "metadata"


def module_from_artifact(path: Path | PurePosixPath) -> str | None:
    """Return the Lean module represented by an artifact-relative path."""
    value = PurePosixPath(path.as_posix())
    name = value.as_posix()
    for suffix in (".olean.private", ".olean.server", ".ir.sig", ".olean", ".ilean", ".ir"):
        if name.endswith(suffix):
            return name[: -len(suffix)].replace("/", ".")
    return None


@dataclass(frozen=True, slots=True)
class CapsuleArtifact:
    path: str
    digest: str
    size: int
    capability: ArtifactCapability

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "digest": self.digest,
            "size": self.size,
            "capability": self.capability,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapsuleArtifact:
        path = str(value.get("path", ""))
        digest = str(value.get("digest", ""))
        size = value.get("size")
        capability = value.get("capability")
        normalized = PurePosixPath(path)
        if (
            not path
            or normalized.is_absolute()
            or ".." in normalized.parts
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or size < 0
            or capability not in {"check", "native", "editor", "development", "metadata"}
        ):
            raise EnvironmentError("capsule contains an invalid artifact record")
        return cls(path, digest, size, capability)


@dataclass(frozen=True, slots=True)
class CapsuleModule:
    name: str
    package: str
    imports: tuple[str, ...]
    imports_complete: bool
    artifacts: tuple[CapsuleArtifact, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "package": self.package,
            "imports": list(self.imports),
            "imports_complete": self.imports_complete,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapsuleModule:
        name = value.get("name")
        package = value.get("package")
        imports = value.get("imports")
        imports_complete = value.get("imports_complete")
        artifacts = value.get("artifacts")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(package, str)
            or not package
            or not isinstance(imports, list)
            or not all(isinstance(item, str) and item for item in imports)
            or not isinstance(imports_complete, bool)
            or not isinstance(artifacts, list)
            or not all(isinstance(item, dict) for item in artifacts)
        ):
            raise EnvironmentError("capsule contains an invalid module record")
        return cls(
            name,
            package,
            tuple(sorted(set(imports))),
            imports_complete,
            tuple(CapsuleArtifact.from_dict(item) for item in artifacts),
        )


@dataclass(frozen=True, slots=True)
class CapsuleManifest:
    environment_id: str
    lock_id: str
    toolchain: str
    modules: tuple[CapsuleModule, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CAPSULE_SCHEMA,
            "environment_id": self.environment_id,
            "lock_id": self.lock_id,
            "toolchain": self.toolchain,
            "modules": [module.to_dict() for module in self.modules],
        }

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapsuleManifest:
        modules = value.get("modules")
        if value.get("schema") != CAPSULE_SCHEMA or not isinstance(modules, list):
            raise EnvironmentError("unsupported check capsule schema")
        result = cls(
            environment_id=str(value.get("environment_id", "")),
            lock_id=str(value.get("lock_id", "")),
            toolchain=str(value.get("toolchain", "")),
            modules=tuple(
                CapsuleModule.from_dict(item) for item in modules if isinstance(item, dict)
            ),
        )
        names = [module.name for module in result.modules]
        artifact_paths = [
            artifact.path for module in result.modules for artifact in module.artifacts
        ]
        if (
            not result.environment_id
            or not result.lock_id
            or not result.toolchain
            or len(result.modules) != len(modules)
            or names != sorted(set(names))
            or len(artifact_paths) != len(set(artifact_paths))
        ):
            raise EnvironmentError("check capsule identity or module ordering is invalid")
        return result

    @classmethod
    def load(cls, path: Path) -> CapsuleManifest:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EnvironmentError(f"could not read check capsule manifest: {path}") from exc
        if not isinstance(value, dict):
            raise EnvironmentError("check capsule manifest must be a JSON object")
        return cls.from_dict(value)

    def closure(self, roots: Iterable[str]) -> tuple[CapsuleModule, ...]:
        """Return the deterministic transitive closure of module roots."""
        modules = {module.name: module for module in self.modules}
        selected: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in selected or name not in modules:
                return
            if name in visiting:
                raise EnvironmentError(f"capsule module graph contains a cycle at {name}")
            visiting.add(name)
            if not modules[name].imports_complete:
                raise EnvironmentError(
                    f"capsule has no authoritative import inventory for module {name}"
                )
            for imported in modules[name].imports:
                visit(imported)
            visiting.remove(name)
            selected.add(name)

        for root in roots:
            visit(root)
        return tuple(modules[name] for name in sorted(selected))


def parse_import_headers(
    lean_command: Sequence[str], source_paths: Sequence[Path], *, batch_size: int = 400
) -> dict[Path, tuple[str, ...]]:
    """Parse module headers with the selected Lean version's own parser."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    result: dict[Path, tuple[str, ...]] = {}
    with tempfile.TemporaryDirectory(prefix="lean-runtime-imports-") as raw:
        helper = Path(raw) / "ParseImports.lean"
        helper.write_text(IMPORT_PARSER_SOURCE, encoding="utf-8")
        for offset in range(0, len(source_paths), batch_size):
            batch = list(source_paths[offset : offset + batch_size])
            process = subprocess.run(
                [*lean_command, "--run", str(helper), *(str(path) for path in batch)],
                text=True,
                capture_output=True,
                check=False,
            )
            if process.returncode:
                raise EnvironmentError(
                    "Lean could not inventory capsule imports: "
                    + (process.stdout + process.stderr)[-4000:]
                )
            try:
                document = json.loads(process.stdout)
                rows = document["imports"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise EnvironmentError("Lean returned an invalid import inventory") from exc
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise EnvironmentError("Lean returned an incomplete import inventory")
            for path, row in zip(batch, rows, strict=True):
                if not isinstance(row, dict) or row.get("errors"):
                    detail = row.get("errors") if isinstance(row, dict) else row
                    raise EnvironmentError(f"could not parse imports for {path}: {detail}")
                imports = row.get("result", {}).get("imports")
                if not isinstance(imports, list):
                    raise EnvironmentError(f"Lean omitted imports for {path}")
                names = {
                    str(item["module"])
                    for item in imports
                    if isinstance(item, dict) and isinstance(item.get("module"), str)
                }
                result[path] = tuple(sorted(names))
    return result


def render_setup(
    *,
    lean_version: str,
    name: str,
    package: str,
    import_artifacts: Mapping[str, Sequence[Sequence[str]]],
    plugins: Sequence[str] = (),
    dynlibs: Sequence[str] = (),
) -> dict[str, Any]:
    """Render normalized artifact facets in Lean's versioned setup dialect."""
    match = _LEAN_VERSION.search(lean_version)
    if match is None:
        raise ValueError(f"unsupported Lean version: {lean_version!r}")
    grouped = (int(match.group(1)), int(match.group(2))) >= (4, 33)
    rendered: dict[str, Any] = {}
    for module, groups in sorted(import_artifacts.items()):
        normalized = [list(group) for group in groups if group]
        if grouped:
            rendered[module] = normalized
        else:
            # Lean <= 4.32's flat dialect orders the public olean, IR, then
            # server/private olean facets. The 4.33 grouped dialect keeps all
            # olean facets together. Preserve that semantic distinction.
            olean = normalized[0] if normalized else []
            public = [path for path in olean if path.endswith(".olean")]
            auxiliary = [path for path in olean if not path.endswith(".olean")]
            remainder = [path for group in normalized[1:] for path in group]
            rendered[module] = [*public, *remainder, *auxiliary]
    return {
        "plugins": list(plugins),
        "package": package,
        "options": {},
        "name": name,
        "isModule": False,
        "importArts": rendered,
        "dynlibs": list(dynlibs),
    }


def setup_artifact_groups(
    manifest: CapsuleManifest,
    workspace: Path,
    roots: Iterable[str],
) -> dict[str, tuple[tuple[str, ...], ...]]:
    """Map a manifest closure to semantically ordered Lean setup facets."""
    result: dict[str, tuple[tuple[str, ...], ...]] = {}
    for module in manifest.closure(roots):
        olean: list[str] = []
        ir: list[str] = []
        for artifact in module.artifacts:
            if artifact.capability != "check":
                continue
            absolute = str(workspace.joinpath(*PurePosixPath(artifact.path).parts))
            if artifact.path.endswith((".ir", ".ir.sig")):
                ir.append(absolute)
            elif artifact.path.endswith((".olean", ".olean.server", ".olean.private")):
                olean.append(absolute)
        olean.sort(
            key=lambda path: (
                0 if path.endswith(".olean") else (1 if path.endswith(".olean.server") else 2)
            )
        )
        ir.sort(key=lambda path: 0 if path.endswith(".ir.sig") else 1)
        groups = tuple(group for group in (tuple(olean), tuple(ir)) if group)
        if groups:
            result[module.name] = groups
    return result


def build_manifest(
    *,
    workspace: Path,
    environment_id: str,
    lock_id: str,
    toolchain: str,
    build_roots: Mapping[str, Path],
    imports: Mapping[str, Sequence[str]],
    complete_modules: frozenset[str] | None = None,
) -> CapsuleManifest:
    """Inventory retained module artifacts beneath package build roots."""
    modules: dict[str, tuple[str, list[CapsuleArtifact]]] = {}
    for package, root in sorted(build_roots.items()):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            module = module_from_artifact(path.relative_to(root))
            if module is None:
                continue
            relative = path.relative_to(workspace).as_posix()
            capability = artifact_capability(path)
            artifact = CapsuleArtifact(
                relative, _digest_path(path), path.stat().st_size, capability
            )
            existing = modules.get(module)
            if existing is not None and existing[0] != package:
                raise EnvironmentError(
                    f"capsule module {module} is provided by both {existing[0]} and {package}"
                )
            modules.setdefault(module, (package, []))[1].append(artifact)
    known_complete = frozenset(imports) if complete_modules is None else complete_modules
    records = tuple(
        CapsuleModule(
            name,
            package,
            tuple(sorted(set(imports.get(name, ())))),
            name in known_complete,
            tuple(sorted(artifacts, key=lambda item: item.path)),
        )
        for name, (package, artifacts) in sorted(modules.items())
    )
    return CapsuleManifest(environment_id, lock_id, toolchain, records)


def _package_directories(
    workspace: Path, lock: EnvironmentLock
) -> tuple[dict[str, Path], dict[str, Path]]:
    raw_packages_dir = lock.manifest.get("packagesDir", ".lake/packages")
    if not isinstance(raw_packages_dir, str):
        raise EnvironmentError("lock packagesDir must be a relative string")
    packages_dir = PurePosixPath(raw_packages_dir)
    if packages_dir.is_absolute() or ".." in packages_dir.parts:
        raise EnvironmentError("lock packagesDir must be a safe relative path")
    sources = {"__root__": workspace}
    builds = {"__root__": workspace / ".lake" / "build" / "lib" / "lean"}
    package_base = workspace.joinpath(*packages_dir.parts)
    for package in lock.packages:
        source = package_base / package.name
        if package.subdir:
            subdir = PurePosixPath(package.subdir)
            if subdir.is_absolute() or ".." in subdir.parts:
                raise EnvironmentError(f"unsafe package subdirectory: {package.subdir!r}")
            source = source.joinpath(*subdir.parts)
        sources[package.name] = source
        builds[package.name] = source / ".lake" / "build" / "lib" / "lean"
    return sources, builds


def inventory_workspace(
    workspace: Path,
    lock: EnvironmentLock,
    environment_id: str,
    lean_command: Sequence[str],
) -> CapsuleManifest:
    """Build an exact capsule inventory from a verified full workspace."""
    source_roots, build_roots = _package_directories(workspace, lock)
    modules_by_package: dict[str, set[str]] = {}
    for package, root in build_roots.items():
        modules_by_package[package] = {
            module
            for path in root.rglob("*.olean")
            if (module := module_from_artifact(path.relative_to(root))) is not None
        }

    source_modules: dict[Path, str] = {}
    claimed: dict[str, tuple[int, Path]] = {}
    for package, root in source_roots.items():
        if not root.is_dir():
            continue
        available = modules_by_package[package]
        for path in sorted(root.rglob("*.lean")):
            relative = path.relative_to(root)
            if ".lake" in relative.parts:
                continue
            stem = relative.with_suffix("")
            candidates = [
                (offset, ".".join(stem.parts[offset:])) for offset in range(len(stem.parts))
            ]
            matches = [candidate for candidate in candidates if candidate[1] in available]
            if not matches:
                continue
            stripped, module = min(matches)
            previous = claimed.get(module)
            if previous is not None and previous[0] < stripped:
                continue
            if previous is not None and previous[0] == stripped and previous[1] != path:
                raise EnvironmentError(
                    f"multiple sources claim capsule module {module}: {previous[1]} and {path}"
                )
            if previous is not None:
                source_modules.pop(previous[1], None)
            claimed[module] = (stripped, path)
            source_modules[path] = module

    parsed = parse_import_headers(lean_command, tuple(source_modules))
    imports = {source_modules[path]: names for path, names in parsed.items()}
    return build_manifest(
        workspace=workspace,
        environment_id=environment_id,
        lock_id=lock.lock_id,
        toolchain=lock.toolchain,
        build_roots=build_roots,
        imports=imports,
        complete_modules=frozenset(imports),
    )


def materialize_capsule(
    workspace: Path,
    destination: Path,
    manifest: CapsuleManifest,
    *,
    capabilities: frozenset[ArtifactCapability] = frozenset({"check"}),
) -> int:
    """Physically materialize selected capsule capabilities, then publish atomically."""
    staging = destination.parent / f".{destination.name}.staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    copied = 0
    try:
        for module in manifest.modules:
            for artifact in module.artifacts:
                if artifact.capability not in capabilities:
                    continue
                source = workspace.joinpath(*PurePosixPath(artifact.path).parts)
                if (
                    not source.is_file()
                    or source.stat().st_size != artifact.size
                    or _digest_path(source) != artifact.digest
                ):
                    raise EnvironmentError(
                        f"capsule artifact changed during export: {artifact.path}"
                    )
                target = staging.joinpath(*PurePosixPath(artifact.path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
                copied += artifact.size
        write_json_atomic(staging / CAPSULE_MANIFEST, manifest.to_dict())
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return copied
