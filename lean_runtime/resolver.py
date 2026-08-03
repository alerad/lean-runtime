"""Compile exact-Git specifications into portable Lake-backed locks."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .errors import ResolutionError
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
    def __init__(self, toolchains: ToolchainManager, store: EnvironmentStore) -> None:
        self.toolchains = toolchains
        self.store = store

    def resolve(self, spec: EnvironmentSpec, *, timeout: float = 900) -> EnvironmentLock:
        toolchain = self.toolchains.ensure(spec.toolchain)
        root_lakefile = generate_lakefile(spec)
        root_module = generate_root_module(spec)
        resolution_root = self.store.home / "resolution"
        resolution_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="resolve-", dir=resolution_root) as raw:
            workspace = Path(raw)
            (workspace / "lean-toolchain").write_text(toolchain + "\n", encoding="utf-8")
            (workspace / "lakefile.toml").write_text(root_lakefile, encoding="utf-8")
            (workspace / f"{ROOT_MODULE}.lean").write_text(root_module, encoding="utf-8")
            command = self.toolchains.command(toolchain, "lake", "update")
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
            return lock

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
            declared_toolchain = package_root / "lean-toolchain"
            if declared_toolchain.is_file():
                declared = declared_toolchain.read_text(encoding="utf-8").strip()
                if declared and normalize_toolchain(declared) != toolchain:
                    raise ResolutionError(
                        f"package {name!r} declares {declared}, not {toolchain}",
                        phase="compatibility",
                    )
            requested = direct.get(name)
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
            locked.append(
                LockedPackage(
                    name=name,
                    url=url,
                    requested_revision=(
                        requested.rev.lower() if requested else value.get("inputRev")
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
