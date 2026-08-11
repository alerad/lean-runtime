from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lean_runtime import (
    DiscoveredPackage,
    GitPackage,
    PackageReference,
    Runtime,
    SpecificationError,
)
from lean_runtime.references import discover_package


def _commit_package(path: Path) -> str:
    path.mkdir()
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    (path / "lakefile.toml").write_text(
        'name = "SamplePackage"\n\n'
        "[[lean_lib]]\n"
        'name = "InternalName"\n'
        'roots = ["Sample", "Sample.Extra"]\n'
    )
    (path / "Sample.lean").write_text("def sample := 42\n")
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "package",
        ],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "tag", "v1.0.0"], cwd=path, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def _commit_lean_dsl_package(path: Path) -> str:
    path.mkdir()
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    (path / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage SamplePackage\nlean_lib Sample\n"
    )
    (path / "Sample.lean").write_text("def sample := 42\n")
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "package",
        ],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "tag", "v1.0.0"], cwd=path, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


class _TranslatingToolchains:
    environment: dict[str, str] = {}

    def command(self, toolchain: str, executable: str, *args: str) -> list[str]:
        assert toolchain == "leanprover/lean4:v4.32.2"
        assert executable == "lake"
        assert args[:2] == ("translate-config", "toml")
        output = args[2]
        source = (
            "from pathlib import Path; "
            f"Path({output!r}).write_text("
            '\'name = \\"SamplePackage\\"\\n\\n[[lean_lib]]\\nname = \\"Sample\\"\\n\')'
        )
        return [sys.executable, "-c", source]


def test_github_reference_is_canonical() -> None:
    reference = PackageReference.parse("github:alerad/leancert@v4.32.2.4")
    assert reference.url == "https://github.com/alerad/leancert.git"
    assert reference.revision == "v4.32.2.4"
    assert reference.revision_kind == "tag"
    assert reference.display == "github:alerad/leancert@v4.32.2.4"


def test_friendly_alias_and_owner_repository_references_are_canonical() -> None:
    mathlib = PackageReference.parse("mathlib@v4.32.2")
    assert mathlib.url == "https://github.com/leanprover-community/mathlib4.git"
    assert mathlib.artifact_command == ("lake", "exe", "cache", "get")
    explicit = PackageReference.parse("alerad/leancert@v4.32.2.4")
    assert explicit.url == "https://github.com/alerad/leancert.git"
    assert explicit.display == "github:alerad/leancert@v4.32.2.4"


def test_unknown_alias_requires_an_explicit_repository() -> None:
    with pytest.raises(SpecificationError, match="Did you mean 'mathlib'"):
        PackageReference.parse("mathilb@v1")


@pytest.mark.parametrize(
    "value",
    ["alerad/leancert", "github:alerad/leancert", "github:/leancert@v1", "github:a/b@main^"],
)
def test_invalid_github_reference_is_rejected(value: str) -> None:
    with pytest.raises(SpecificationError, match="package reference"):
        PackageReference.parse(value)


def test_package_discovery_pins_and_reads_lake_metadata(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    revision = _commit_package(repository)
    reference = PackageReference.git(repository.as_uri(), "v1.0.0")
    discovered = discover_package(reference, directory=tmp_path / "discovery")
    assert discovered.toolchain == "leanprover/lean4:v4.32.2"
    assert discovered.package.name == "SamplePackage"
    assert discovered.package.rev == revision
    assert discovered.package.revision_kind == "commit"
    assert discovered.package.module == "Sample"


def test_package_discovery_translates_lake_dsl_with_declared_toolchain(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    revision = _commit_lean_dsl_package(repository)
    reference = PackageReference.git(repository.as_uri(), "v1.0.0")
    discovered = discover_package(
        reference,
        directory=tmp_path / "discovery",
        toolchains=_TranslatingToolchains(),  # type: ignore[arg-type]
    )
    assert discovered.toolchain == "leanprover/lean4:v4.32.2"
    assert discovered.package.name == "SamplePackage"
    assert discovered.package.rev == revision
    assert discovered.package.module == "Sample"


def test_reference_toolchains_must_agree_without_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    references = [
        PackageReference.git("https://example.com/one.git", "v1"),
        PackageReference.git("https://example.com/two.git", "v2"),
    ]

    def discover(
        reference: PackageReference, *, directory: Path, toolchains: object
    ) -> DiscoveredPackage:
        del directory, toolchains
        index = references.index(reference) + 1
        return DiscoveredPackage(
            reference,
            f"leanprover/lean4:v4.3{index}.0",
            GitPackage.git(
                f"package{index}",
                reference.url,
                str(index) * 40,
                root_module=f"Package{index}",
            ),
        )

    monkeypatch.setattr("lean_runtime.runtime.discover_package", discover)
    runtime = Runtime(home=tmp_path / "runtime")
    with pytest.raises(SpecificationError, match="different Lean toolchains"):
        runtime.spec_from_references(references)

    spec = runtime.spec_from_references(references, toolchain="4.32.0")
    assert spec.toolchain == "leanprover/lean4:v4.32.0"
    assert [package.name for package in spec.packages] == ["package1", "package2"]


def test_artifact_accelerators_key_canonical_urls_only() -> None:
    from lean_runtime.references import artifact_accelerators

    accelerators = artifact_accelerators()
    assert accelerators["https://github.com/leanprover-community/mathlib4.git"] == (
        "lake",
        "exe",
        "cache",
        "get",
    )
    # Aliases without a cache command must not appear.
    assert not any("leancert" in url for url in accelerators)
    # A fork sharing the package name is keyed out by URL.
    assert "https://github.com/someone-else/mathlib4.git" not in accelerators
