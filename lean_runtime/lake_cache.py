"""Capability-probed integration with Lake's native artifact cache."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

if TYPE_CHECKING:
    from .events import EventEmitter
    from .models import PackageProvenance
    from .projects import ProjectContext
    from .toolchains import ToolchainManager

from .decisions import Decision
from .references import artifact_accelerators
from .serialization import sha256_id
from .store import platform_compatibility
from .toolchains import normalize_toolchain


@dataclass(frozen=True, slots=True)
class LakeCacheCapabilities:
    """Observed Lake interfaces; never inferred from a version string."""

    supported: bool
    build_mappings: bool
    cache_add: bool
    restore_hit_signals: tuple[str, ...]
    build_help_digest: str
    cache_help_digest: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LakeCacheCapabilities:
        return cls(
            supported=bool(value["supported"]),
            build_mappings=bool(value["build_mappings"]),
            cache_add=bool(value["cache_add"]),
            restore_hit_signals=tuple(str(item) for item in value["restore_hit_signals"]),
            build_help_digest=str(value["build_help_digest"]),
            cache_help_digest=str(value["cache_help_digest"]),
        )


class LakeArtifactCache:
    """Own Lake cache capability decisions and ABI-isolated local storage.

    Only root packages that explicitly enable Lake's artifact cache use this
    service. Dependencies remain in Lean Runtime's shared workspace rather than
    being copied into a second Lake cache.
    """

    _HIT_SIGNALS = (
        "found artifact in cache",
        "restored artifact from cache",
        "downloaded artifact",
    )

    def __init__(
        self,
        home: Path,
        toolchains: ToolchainManager,
        events: EventEmitter,
    ) -> None:
        self.home = home
        self.toolchains = toolchains
        self.events = events
        self._memory: dict[str, LakeCacheCapabilities] = {}

    def _identity(self, toolchain: str) -> dict[str, Any]:
        digest_method = getattr(self.toolchains, "executable_digest", None)
        executable_digest = (
            str(digest_method(toolchain, "lake"))
            if callable(digest_method)
            else f"toolchain:{normalize_toolchain(toolchain)}"
        )
        return {
            "toolchain": normalize_toolchain(toolchain),
            "lake_executable_digest": executable_digest,
            "platform": platform_compatibility(),
        }

    def key(self, toolchain: str) -> str:
        return sha256_id("lake-cache", self._identity(toolchain)).removeprefix("lake-cache-")

    def cache_root(self, toolchain: str) -> Path:
        return self.home / "lake-artifacts" / self.key(toolchain)

    def _capability_path(self, toolchain: str) -> Path:
        return self.home / "lake-capabilities" / f"{self.key(toolchain)}.json"

    @staticmethod
    def _digest(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

    def capabilities(self, toolchain: str) -> LakeCacheCapabilities:
        """Probe once per exact toolchain/platform identity, then stay silent."""
        identity = self.key(toolchain)
        if identity in self._memory:
            return self._memory[identity]
        path = self._capability_path(toolchain)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if loaded.get("identity") == self._identity(toolchain):
                capabilities = LakeCacheCapabilities.from_dict(loaded["capabilities"])
                self._memory[identity] = capabilities
                return capabilities
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

        self.events.emit(
            "lake_cache.capability_probe_started",
            "Checking Lake artifact-cache support",
            phase="project.prepare",
            toolchain=normalize_toolchain(toolchain),
        )
        build = self._help(toolchain, "build", "--help")
        cache = self._help(toolchain, "cache", "add", "--help")
        build_mappings = build[0] and "-o" in build[1] and "mappings" in build[1]
        cache_add = cache[0] and "--service" in cache[1] and "mappings" in cache[1]
        capabilities = LakeCacheCapabilities(
            supported=build_mappings and cache_add,
            build_mappings=build_mappings,
            cache_add=cache_add,
            restore_hit_signals=self._HIT_SIGNALS,
            build_help_digest=self._digest(build[1]),
            cache_help_digest=self._digest(cache[1]),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"identity": self._identity(toolchain), "capabilities": asdict(capabilities)},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self._memory[identity] = capabilities
        self.events.emit(
            "lake_cache.capability_probe_finished",
            "Lake artifact-cache capability recorded",
            phase="project.prepare",
            supported=capabilities.supported,
            toolchain=normalize_toolchain(toolchain),
        )
        return capabilities

    def _help(self, toolchain: str, *arguments: str) -> tuple[bool, str]:
        try:
            process = subprocess.run(
                self.toolchains.command(toolchain, "lake", *arguments),
                env=self.toolchains.environment_for(toolchain),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, ""
        return process.returncode == 0, process.stdout

    def project_opted_in(self, context: ProjectContext) -> bool:
        if context.lakefile.name != "lakefile.toml":
            return False
        try:
            with context.lakefile.open("rb") as stream:
                value = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError):
            return False
        return value.get("enableArtifactCache") is True

    def environment(self, context: ProjectContext) -> dict[str, str]:
        """Return root-only cache settings without caching dependency packages."""
        environment = self.toolchains.environment_for(context.toolchain)
        if not self.project_opted_in(context):
            return environment
        self.capabilities(context.toolchain)
        root = self.cache_root(context.toolchain)
        root.mkdir(parents=True, exist_ok=True)
        environment.update(
            {
                "LAKE_ARTIFACT_CACHE": "false",
                "LAKE_RESTORE_ARTIFACTS": "true",
                "LAKE_CACHE_DIR": str(root),
            }
        )
        return environment

    @staticmethod
    def dependency_accelerators(
        packages: Sequence[PackageProvenance],
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Return trusted cache commands for exact dependencies in a project graph.

        Accelerators are keyed by canonical repository URL.  Package names are
        deliberately not sufficient: a fork named ``mathlib`` must not gain the
        upstream project's executable as an implicit build step.
        """

        known = {
            url.lower().removesuffix(".git"): command
            for url, command in artifact_accelerators().items()
        }
        selected: list[tuple[str, tuple[str, ...]]] = []
        for package in packages:
            command = known.get(package.url.lower().removesuffix(".git"))
            if command:
                selected.append((package.name, command))
        return tuple(selected)

    def decision(self, context: ProjectContext) -> Decision:
        opted_in = self.project_opted_in(context)
        capabilities = self.capabilities(context.toolchain) if opted_in else None
        enabled = opted_in and capabilities is not None and capabilities.supported
        return Decision(
            "lake_artifact_cache",
            str(context.root),
            "accepted" if enabled else "skipped",
            reason=None if enabled else ("project_not_opted_in" if not opted_in else "unsupported"),
            details={
                "scope": "root-package-only",
                "cache_root": str(self.cache_root(context.toolchain)) if enabled else None,
                "remote_mappings": "disabled_without_sha256_inventory",
            },
        )
