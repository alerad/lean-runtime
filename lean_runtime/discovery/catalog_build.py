"""Build canonical discovery catalogs from exact-lock source manifests."""

from __future__ import annotations

from pathlib import Path

from ..errors import LeanRuntimeError
from ..lockfiles import EnvironmentLock
from ..runtime import Runtime
from .catalog import Catalog, CatalogEntry
from .catalog_manifest import CatalogSourceEntry, CatalogSourceManifest
from .errors import CatalogError
from .module_inventory import SourceInventoryUnavailable, modules_for_lock


def _resolve_references(entry: CatalogSourceEntry, runtime: Runtime) -> EnvironmentLock:
    """Resolve catalog sources without consulting the catalog being rebuilt."""

    try:
        spec = runtime.spec_from_references(entry.references)
        return runtime.prepare(spec)
    except LeanRuntimeError as exc:
        raise CatalogError(f"could not resolve entry {entry.id!r}: {exc}") from exc


def _lock(entry: CatalogSourceEntry, runtime: Runtime) -> EnvironmentLock:
    if entry.lock_path.is_file():
        return EnvironmentLock.load(entry.lock_path)
    if not entry.references:
        raise CatalogError(
            f"entry {entry.id!r} lock does not exist and no references were supplied"
        )
    lock = _resolve_references(entry, runtime)
    entry.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock.write(entry.lock_path)
    return lock


def build_catalog(manifest: CatalogSourceManifest, *, runtime: Runtime) -> Catalog:
    """Resolve missing locks once and build one validated canonical catalog."""

    entries: list[CatalogEntry] = []
    for source in manifest.entries:
        lock = _lock(source, runtime)
        modules = set(source.modules)
        if source.inventory_packages:
            try:
                modules.update(modules_for_lock(runtime, lock, source.inventory_packages))
            except SourceInventoryUnavailable:
                if not source.references:
                    raise
                resolved = _resolve_references(source, runtime)
                if resolved.lock_id != lock.lock_id:
                    raise CatalogError(
                        f"entry {source.id!r} references no longer resolve to its frozen lock; "
                        f"expected {lock.lock_id}, observed {resolved.lock_id}"
                    ) from None
                modules.update(modules_for_lock(runtime, lock, source.inventory_packages))
        entries.append(
            CatalogEntry(
                id=source.id,
                channel=source.channel,
                toolchain=lock.toolchain,
                lock=lock,
                modules=frozenset(modules),
                library_hints=source.library_hints,
                created_at=source.created_at,
            )
        )
    return Catalog(generated_at=manifest.generated_at, entries=tuple(entries))


def build_catalog_file(
    manifest_path: str | Path,
    output: str | Path,
    *,
    runtime: Runtime,
) -> Catalog:
    """Build, write, and reload a catalog through its public serialization boundary."""

    catalog = build_catalog(CatalogSourceManifest.from_file(manifest_path), runtime=runtime)
    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    catalog.write(destination)
    loaded = Catalog.from_file(destination)
    if loaded.digest != catalog.digest:
        raise CatalogError("written catalog digest differs after reload")
    return loaded
