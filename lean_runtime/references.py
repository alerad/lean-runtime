"""Ergonomic package references compiled into exact Git package specifications."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from .errors import ResolutionError, SpecificationError
from .specs import GitPackage
from .toolchains import ToolchainManager, normalize_toolchain

_GITHUB = re.compile(
    r"github:(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9_.-]+)@(?P<revision>[A-Za-z0-9][A-Za-z0-9._/+\-]{0,199})"
)
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")


@dataclass(frozen=True, slots=True)
class PackageReference:
    """A human-friendly reference to one Git-hosted Lean package."""

    url: str
    revision: str
    display: str
    revision_kind: Literal["commit", "tag"]

    @classmethod
    def parse(cls, value: str) -> PackageReference:
        """Parse ``github:owner/repository@tag-or-commit``."""
        match = _GITHUB.fullmatch(value)
        if match is None:
            raise SpecificationError(
                "invalid package reference; expected github:owner/repository@tag-or-commit"
            )
        owner = match.group("owner")
        repository = match.group("repo")
        if repository.endswith(".git"):
            repository = repository[:-4]
        if not repository:
            raise SpecificationError("GitHub package reference has an empty repository name")
        revision = match.group("revision")
        kind: Literal["commit", "tag"] = (
            "commit" if _COMMIT.fullmatch(revision) is not None else "tag"
        )
        return cls(
            url=f"https://github.com/{owner}/{repository}.git",
            revision=revision,
            display=f"github:{owner}/{repository}@{revision}",
            revision_kind=kind,
        )

    @classmethod
    def git(cls, url: str, revision: str) -> PackageReference:
        """Construct a reference for a generic Git URL, primarily for Python callers."""
        if not url or "\n" in url or "\r" in url:
            raise SpecificationError("invalid Git package-reference URL")
        kind: Literal["commit", "tag"] = (
            "commit" if _COMMIT.fullmatch(revision) is not None else "tag"
        )
        # Reuse GitPackage's strict revision validation with a temporary valid name.
        if kind == "commit":
            GitPackage.git("package_reference", url, revision)
        else:
            GitPackage.tag("package_reference", url, revision)
        return cls(url=url, revision=revision, display=f"git:{url}@{revision}", revision_kind=kind)


@dataclass(frozen=True, slots=True)
class DiscoveredPackage:
    """Exact package metadata discovered from a referenced Lake project."""

    reference: PackageReference
    toolchain: str
    package: GitPackage


def _run_git(arguments: list[str], *, cwd: Path | None = None, timeout: float = 120) -> str:
    try:
        process = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ResolutionError(
            "package-reference Git operation timed out",
            phase="package-discovery",
            command=("git", *arguments),
            exit_code=124,
            output=str(exc.stdout or "") + str(exc.stderr or ""),
        ) from exc
    if process.returncode:
        raise ResolutionError(
            "could not acquire package-reference metadata",
            phase="package-discovery",
            command=("git", *arguments),
            exit_code=process.returncode,
            output=process.stdout + process.stderr,
        )
    return process.stdout.strip()


def _read_lake_metadata(lakefile: Path) -> tuple[str, str]:
    """Read the declarative Lake metadata needed by environment resolution."""
    try:
        value = tomllib.loads(lakefile.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SpecificationError(f"could not read package metadata {lakefile.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise SpecificationError(f"package metadata {lakefile.name} is not a TOML object")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise SpecificationError(f"package metadata {lakefile.name} has no package name")
    libraries = value.get("lean_lib")
    if not isinstance(libraries, list) or not libraries:
        raise SpecificationError(f"package metadata {lakefile.name} declares no [[lean_lib]]")
    first = libraries[0]
    if not isinstance(first, dict):
        raise SpecificationError("package's first [[lean_lib]] is malformed")
    roots = first.get("roots")
    root_module: Any = roots[0] if isinstance(roots, list) and roots else first.get("name")
    if not isinstance(root_module, str) or not root_module:
        raise SpecificationError("package's first [[lean_lib]] has no importable root")
    return name, root_module


def _lake_metadata(
    checkout: Path,
    *,
    toolchain: str,
    toolchains: ToolchainManager,
) -> tuple[str, str]:
    lakefile = checkout / "lakefile.toml"
    if lakefile.is_file():
        return _read_lake_metadata(lakefile)

    lakefile_lean = checkout / "lakefile.lean"
    if not lakefile_lean.is_file():
        raise SpecificationError(
            "referenced package has neither a root lakefile.toml nor lakefile.lean"
        )
    translated = checkout / ".lean-runtime-lakefile.toml"
    try:
        command = toolchains.command(
            toolchain,
            "lake",
            "translate-config",
            "toml",
            str(translated),
        )
        process = subprocess.run(
            command,
            cwd=checkout,
            env=toolchains.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ResolutionError(
            "Lake package-metadata translation timed out",
            phase="package-discovery",
            command=tuple(command),
            exit_code=124,
            output=str(exc.stdout or "") + str(exc.stderr or ""),
        ) from exc
    if process.returncode:
        raise ResolutionError(
            "Lake could not translate package metadata",
            phase="package-discovery",
            command=tuple(command),
            exit_code=process.returncode,
            output=process.stdout,
        )
    if not translated.is_file():
        raise ResolutionError(
            "Lake completed without producing translated package metadata",
            phase="package-discovery",
            command=tuple(command),
            output=process.stdout,
        )
    return _read_lake_metadata(translated)


def discover_package(
    reference: PackageReference,
    *,
    directory: Path,
    toolchains: ToolchainManager | None = None,
) -> DiscoveredPackage:
    """Acquire a shallow checkout and compile its metadata into an exact package."""
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="package-", dir=directory) as raw:
        checkout = Path(raw)
        _run_git(["init", "--quiet"], cwd=checkout)
        _run_git(["remote", "add", "origin", reference.url], cwd=checkout)
        target = (
            reference.revision
            if reference.revision_kind == "commit"
            else f"refs/tags/{reference.revision}"
        )
        _run_git(["fetch", "--quiet", "--depth", "1", "origin", target], cwd=checkout)
        _run_git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=checkout)
        revision = _run_git(["rev-parse", "HEAD"], cwd=checkout).lower()
        if _COMMIT.fullmatch(revision) is None:
            raise ResolutionError(
                "package reference did not resolve to a full Git commit",
                phase="package-discovery",
            )
        toolchain_path = checkout / "lean-toolchain"
        if not toolchain_path.is_file():
            raise SpecificationError("referenced package has no lean-toolchain file")
        toolchain = normalize_toolchain(toolchain_path.read_text(encoding="utf-8"))
        manager = toolchains or ToolchainManager(directory.parent)
        name, root_module = _lake_metadata(
            checkout,
            toolchain=toolchain,
            toolchains=manager,
        )
        package = GitPackage.git(name, reference.url, revision, root_module=root_module)
        return DiscoveredPackage(reference=reference, toolchain=toolchain, package=package)


def normalize_references(
    values: Sequence[str | PackageReference],
) -> tuple[PackageReference, ...]:
    return tuple(
        PackageReference.parse(value) if isinstance(value, str) else value for value in values
    )
