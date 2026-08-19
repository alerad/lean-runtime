"""Importable modules from exact compiled environments with source fallback."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from ..capsules import CAPSULE_MANIFEST, module_from_artifact
from ..errors import EnvironmentError
from ..lockfiles import EnvironmentLock, LockedPackage
from ..runtime import Runtime
from ..store import environment_identity
from .errors import CatalogError

_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")


class SourceInventoryUnavailable(CatalogError):
    """An exact lock is available but its validated sources are not local."""


def modules_from_compiled_environment(
    runtime: Runtime,
    lock: EnvironmentLock,
    package_names: tuple[str, ...],
) -> frozenset[str] | None:
    """Read importable modules from a retained authoritative full build."""

    workspace = runtime.store.environment_path(environment_identity(lock)) / "workspace"
    if not workspace.is_dir() or (workspace / CAPSULE_MANIFEST).is_file():
        return None
    raw_packages_dir = lock.manifest.get("packagesDir", ".lake/packages")
    if not isinstance(raw_packages_dir, str):
        raise CatalogError("lock packagesDir must be a relative string")
    packages_dir = PurePosixPath(raw_packages_dir)
    if packages_dir.is_absolute() or ".." in packages_dir.parts:
        raise CatalogError("lock packagesDir must be a safe relative string")
    packages = {package.name: package for package in lock.packages}
    missing = sorted(set(package_names) - set(packages))
    if missing:
        raise CatalogError(f"lock {lock.lock_id} lacks inventory packages: {', '.join(missing)}")
    modules: set[str] = set()
    package_base = workspace.joinpath(*packages_dir.parts)
    for name in package_names:
        package = packages[name]
        source = package_base / package.name
        if package.subdir:
            subdir = PurePosixPath(package.subdir)
            if subdir.is_absolute() or ".." in subdir.parts:
                raise CatalogError(f"unsafe package subdirectory: {package.subdir!r}")
            source = source.joinpath(*subdir.parts)
        build_root = source / ".lake" / "build" / "lib" / "lean"
        for path in build_root.rglob("*.olean") if build_root.is_dir() else ():
            module = module_from_artifact(path.relative_to(build_root))
            if module is not None:
                modules.add(module)
    return frozenset(modules) if modules else None


def _module_name(path: Path, *, source_root: Path) -> str | None:
    relative = path.relative_to(source_root).with_suffix("")
    name = ".".join(relative.parts)
    return name if _MODULE.fullmatch(name) is not None else None


def modules_from_source(source: Path, package: LockedPackage) -> frozenset[str]:
    """List modules beneath one locked package's declared root module."""

    if package.root_module is None:
        raise CatalogError(f"locked package {package.name!r} has no root_module metadata")
    package_root = source / package.subdir if package.subdir else source
    root_parts = package.root_module.split(".")
    root_stem = package_root.joinpath(*root_parts)
    candidates: list[Path] = []
    root_file = root_stem.with_suffix(".lean")
    if root_file.is_file():
        candidates.append(root_file)
    if root_stem.is_dir():
        candidates.extend(path for path in root_stem.rglob("*.lean") if path.is_file())
    modules = frozenset(
        module
        for path in candidates
        if (module := _module_name(path, source_root=package_root)) is not None
    )
    if not modules:
        raise CatalogError(
            f"locked package {package.name!r} produced no modules beneath "
            f"root {package.root_module!r}"
        )
    return modules


def modules_for_lock(
    runtime: Runtime,
    lock: EnvironmentLock,
    package_names: tuple[str, ...],
) -> frozenset[str]:
    """Validate exact package sources and inventory selected package modules."""

    compiled = modules_from_compiled_environment(runtime, lock, package_names)
    if compiled is not None:
        return compiled
    packages = {package.name: package for package in lock.packages}
    missing = sorted(set(package_names) - set(packages))
    if missing:
        raise CatalogError(f"lock {lock.lock_id} lacks inventory packages: {', '.join(missing)}")
    modules: set[str] = set()
    for name in package_names:
        package = packages[name]
        try:
            source = runtime.store.validate_source(
                package.source_id,
                url=package.url,
                revision=package.revision,
                tree_hash=package.tree_hash,
            )
        except EnvironmentError as exc:
            raise SourceInventoryUnavailable(
                f"exact source for package {package.name!r} is unavailable; "
                "resolve the manifest references before building the catalog"
            ) from exc
        modules.update(modules_from_source(source, package))
    return frozenset(modules)
