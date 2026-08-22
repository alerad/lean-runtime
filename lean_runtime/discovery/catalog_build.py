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


def build_catalog(
    manifest: CatalogSourceManifest,
    *,
    runtime: Runtime,
    previous: Catalog | None = None,
) -> Catalog:
    """Resolve missing locks once and build one validated canonical catalog.

    A previous catalog lets unchanged entries reuse their module inventories,
    so an incremental rebuild only materializes sources for new or changed
    locks.
    """

    reusable = {} if previous is None else {entry.id: entry for entry in previous.entries}
    entries: list[CatalogEntry] = []
    for source in manifest.entries:
        lock = _lock(source, runtime)
        prior = reusable.get(source.id)
        if (
            prior is not None
            and prior.lock.lock_id == lock.lock_id
            and prior.channel == source.channel
            and set(source.modules) <= prior.modules
        ):
            entries.append(
                CatalogEntry(
                    id=source.id,
                    channel=source.channel,
                    toolchain=lock.toolchain,
                    lock=lock,
                    modules=prior.modules,
                    created_at=source.created_at,
                )
            )
            continue
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
                created_at=source.created_at,
            )
        )
    return Catalog(generated_at=manifest.generated_at, entries=tuple(entries))


def build_catalog_file(
    manifest_path: str | Path,
    output: str | Path,
    *,
    runtime: Runtime,
    previous_path: str | Path | None = None,
) -> Catalog:
    """Build, write, and reload a catalog through its public serialization boundary."""

    previous = None
    if previous_path is not None:
        resolved_previous = Path(previous_path).expanduser().resolve()
        if resolved_previous.is_file():
            previous = Catalog.from_file(resolved_previous)
    catalog = build_catalog(
        CatalogSourceManifest.from_file(manifest_path), runtime=runtime, previous=previous
    )
    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    catalog.write(destination)
    loaded = Catalog.from_file(destination)
    if loaded.digest != catalog.digest:
        raise CatalogError("written catalog digest differs after reload")
    return loaded
