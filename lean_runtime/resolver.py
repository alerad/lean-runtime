"""Compile exact-Git specifications into portable Lake-backed locks."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from .errors import ResolutionError
from .events import EventEmitter
from .lake import ROOT_MODULE, generate_lakefile, generate_root_module
from .lockfiles import EnvironmentLock, LockedPackage
from .serialization import sha256_id
from .specs import EnvironmentSpec, GitPackage
from .store import EnvironmentStore
from .toolchains import ToolchainManager, normalize_toolchain

_COMMIT = re.compile(r"[0-9a-fA-F]{40}")


def _git(path: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(path), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode:
        raise ResolutionError(
            f"could not inspect resolved Git package at {path}",
            phase="lock-validation",
            command=("git", "-C", str(path), *arguments),
            exit_code=process.returncode,
            output=process.stdout + process.stderr,
        )
    return process.stdout.strip()


class EnvironmentResolver:
    def __init__(
        self,
        toolchains: ToolchainManager,
        store: EnvironmentStore,
        events: EventEmitter | None = None,
    ) -> None:
        self.toolchains = toolchains
        self.store = store
        self.events = events or EventEmitter()

    def resolve(self, spec: EnvironmentSpec, *, timeout: float = 900) -> EnvironmentLock:
        self.events.emit(
            "resolution.started",
            "Resolving environment",
            toolchain=spec.toolchain,
            packages=len(spec.packages),
        )
        toolchain = self.toolchains.ensure(spec.toolchain)
        self.events.emit("toolchain.ready", "Lean toolchain is ready", toolchain=toolchain)
        pinned_spec = self._pin_tags(spec)
        root_lakefile = generate_lakefile(pinned_spec)
        root_module = generate_root_module(spec)
        resolution_root = self.store.home / "resolution"
        resolution_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="resolve-", dir=resolution_root) as raw:
            workspace = Path(raw)
            (workspace / "lean-toolchain").write_text(toolchain + "\n", encoding="utf-8")
            (workspace / "lakefile.toml").write_text(root_lakefile, encoding="utf-8")
            (workspace / f"{ROOT_MODULE}.lean").write_text(root_module, encoding="utf-8")
            command = self.toolchains.command(toolchain, "lake", "update")
            self.events.emit("resolution.lake_started", "Lake dependency resolution started")
            try:
                process = subprocess.run(
                    command,
                    cwd=workspace,
                    env=self.toolchains.environment,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ResolutionError(
                    f"Lake resolution exceeded {timeout:g} seconds",
                    command=tuple(command),
                    exit_code=124,
                    output=str(exc.stdout or "") + str(exc.stderr or ""),
                ) from exc
            if process.returncode:
                raise ResolutionError(
                    "Lake could not resolve the environment specification",
                    command=tuple(command),
                    exit_code=process.returncode,
                    output=process.stdout + process.stderr,
                )
            manifest_path = workspace / "lake-manifest.json"
            if not manifest_path.is_file():
                raise ResolutionError("Lake resolution did not produce lake-manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ResolutionError("Lake manifest is not a JSON object")
            packages = self._lock_packages(workspace, manifest, spec, toolchain)
            lock = EnvironmentLock(
                toolchain=toolchain,
                spec_digest=spec.spec_digest,
                root_lakefile=root_lakefile,
                root_module=root_module,
                manifest=manifest,
                packages=packages,
            )
            self.store.publish_lock(lock)
            self.events.emit(
                "resolution.completed",
                "Environment lock published",
                lock_id=lock.lock_id,
                packages=len(packages),
            )
            return lock

    def _pin_tags(self, spec: EnvironmentSpec) -> EnvironmentSpec:
        pinned: list[GitPackage] = []
        for package in spec.packages:
            if package.revision_kind == "commit":
                pinned.append(package)
                continue
            reference = f"refs/tags/{package.rev}"
            command = ["git", "ls-remote", package.url, reference, f"{reference}^{{}}"]
            try:
                process = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ResolutionError(
                    f"tag lookup timed out for package {package.name!r}",
                    phase="tag-resolution",
                    command=tuple(command),
                    exit_code=124,
                    output=str(exc.stdout or "") + str(exc.stderr or ""),
                ) from exc
            if process.returncode:
                raise ResolutionError(
                    f"could not resolve tag {package.rev!r} for package {package.name!r}",
                    phase="tag-resolution",
                    command=tuple(command),
                    exit_code=process.returncode,
                    output=process.stdout + process.stderr,
                )
            candidates: dict[str, str] = {}
            for line in process.stdout.splitlines():
                fields = line.split(maxsplit=1)
                if len(fields) == 2:
                    candidates[fields[1]] = fields[0].lower()
            revision = candidates.get(f"{reference}^{{}}", candidates.get(reference, ""))
            if _COMMIT.fullmatch(revision) is None:
                raise ResolutionError(
                    f"Git tag {package.rev!r} was not found for package {package.name!r}",
                    phase="tag-resolution",
                    command=tuple(command),
                    exit_code=1,
                    output=process.stdout + process.stderr,
                )
            pinned.append(replace(package, rev=revision, revision_kind="commit"))
            self.events.emit(
                "tag.resolved",
                f"Resolved {package.rev} for {package.name}",
                package=package.name,
                tag=package.rev,
                revision=revision,
            )
        return EnvironmentSpec(spec.toolchain, tuple(pinned))

    def _lock_packages(
        self,
        workspace: Path,
        manifest: dict[str, Any],
        spec: EnvironmentSpec,
        toolchain: str,
    ) -> tuple[LockedPackage, ...]:
        direct: dict[str, GitPackage] = {package.name: package for package in spec.packages}
        packages_dir = workspace / str(manifest.get("packagesDir", ".lake/packages"))
        raw_packages = manifest.get("packages", [])
        if not isinstance(raw_packages, list):
            raise ResolutionError("Lake manifest packages field is not an array")
        locked: list[LockedPackage] = []
        for value in raw_packages:
            if not isinstance(value, dict) or value.get("type") != "git":
                raise ResolutionError(
                    "the initial lock schema supports only Git transitive dependencies",
                    phase="lock-validation",
                )
            name = str(value.get("name", ""))
            revision = str(value.get("rev", "")).lower()
            if _COMMIT.fullmatch(revision) is None:
                raise ResolutionError(
                    f"Lake did not resolve package {name!r} to a full Git commit",
                    phase="lock-validation",
                )
            checkout = packages_dir / name
            if not checkout.is_dir():
                raise ResolutionError(
                    f"resolved package checkout is missing: {name}", phase="acquisition"
                )
            head = _git(checkout, "rev-parse", "HEAD").lower()
            if head != revision:
                raise ResolutionError(
                    f"package {name!r} checkout does not match its manifest revision",
                    phase="lock-validation",
                )
            tree = _git(checkout, "rev-parse", "HEAD^{tree}").lower()
            package_root = checkout
            subdir = value.get("subDir")
            if isinstance(subdir, str) and subdir:
                package_root /= subdir
            requested = direct.get(name)
            declared_toolchain = package_root / "lean-toolchain"
            if declared_toolchain.is_file():
                declared = declared_toolchain.read_text(encoding="utf-8").strip()
                if declared and normalize_toolchain(declared) != toolchain:
                    self.events.emit(
                        "compatibility.toolchain_difference",
                        f"{name} declares {declared}; compatibility will be tested by the build",
                        package=name,
                        declared_toolchain=declared,
                        environment_toolchain=toolchain,
                        direct=requested is not None,
                    )
            url = str(value.get("url", ""))
            source_id = sha256_id(
                "source", {"source": "git", "url": url, "revision": revision, "tree": tree}
            )
            metadata = {
                "schema": "lean-runtime-git-source/1",
                "source_id": source_id,
                "name": name,
                "url": url,
                "revision": revision,
                "tree_hash": tree,
            }
            self.store.publish_source(checkout, source_id, metadata)
            self.events.emit(
                "source.locked",
                f"Locked {name}",
                package=name,
                revision=revision,
                source_id=source_id,
            )
            locked.append(
                LockedPackage(
                    name=name,
                    url=url,
                    requested_revision=(
                        (
                            requested.rev.lower()
                            if requested.revision_kind == "commit"
                            else requested.rev
                        )
                        if requested
                        else value.get("inputRev")
                    ),
                    revision=revision,
                    tree_hash=tree,
                    source_id=source_id,
                    inherited=bool(value.get("inherited", False)),
                    subdir=subdir if isinstance(subdir, str) else None,
                    root_module=requested.module if requested else None,
                    artifact_command=requested.artifact_command if requested else (),
                )
            )
        missing = sorted(set(direct) - {package.name for package in locked})
        if missing:
            raise ResolutionError(
                "direct packages missing from Lake manifest: " + ", ".join(missing),
                phase="lock-validation",
            )
        return tuple(sorted(locked, key=lambda package: package.name))
