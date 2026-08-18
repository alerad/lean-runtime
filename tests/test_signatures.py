from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime import EnvironmentError
from lean_runtime.oci import OCIRepository
from lean_runtime.publisher_verification import CosignVerifier


def test_cosign_verification_binds_digest_identity_and_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "verification_tool"
    executable.write_text("")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command[1:3] == ["version", "--json"]:
            return SimpleNamespace(returncode=0, stdout='{"gitVersion":"v3.0.4"}', stderr="")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("lean_runtime.publisher_verification.subprocess.run", run)
    verifier = CosignVerifier(
        "https://github.com/owner/project/.github/workflows/cache.yml@refs/heads/main",
        "https://token.actions.githubusercontent.com",
        executable=executable,
    )
    verifier.verify(OCIRepository.parse("oci://ghcr.io/owner/cache"), "sha256:" + "a" * 64)
    command = commands[-1]
    assert command[-1] == "ghcr.io/owner/cache@sha256:" + "a" * 64
    assert "--certificate-identity" in command
    assert "--certificate-oidc-issuer" in command


def test_vulnerable_cosign_release_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "verification_tool"
    executable.write_text("")
    monkeypatch.setattr(
        "lean_runtime.publisher_verification.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout='{"gitVersion":"v3.0.3"}', stderr=""
        ),
    )
    with pytest.raises(EnvironmentError, match="3.0.4"):
        CosignVerifier("identity", "issuer", executable=executable)


@pytest.mark.parametrize("version", ["v2.6.2", "v2.99.0", "v3.0.4", "v3.9.0"])
def test_supported_cosign_releases_are_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    executable = tmp_path / "verification_tool"
    executable.write_text("")
    monkeypatch.setattr(
        "lean_runtime.publisher_verification.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=f'{{"gitVersion":"{version}"}}', stderr=""
        ),
    )
    CosignVerifier("identity", "issuer", executable=executable)


def test_missing_cosign_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lean_runtime.publisher_verification.shutil.which", lambda _name: None)
    with pytest.raises(EnvironmentError, match="required.*not installed"):
        CosignVerifier("identity", "issuer", executable="definitely-missing-cosign")


def test_publisher_identity_and_issuer_must_be_configured_together() -> None:
    with pytest.raises(ValueError, match="both identity and OIDC issuer"):
        CosignVerifier("identity", None)
    with pytest.raises(ValueError, match="both identity and OIDC issuer"):
        CosignVerifier(None, "issuer")


def test_wrong_publisher_identity_or_issuer_is_an_integrity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "verification_tool"
    executable.write_text("")

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[1:3] == ["version", "--json"]:
            return SimpleNamespace(returncode=0, stdout='{"gitVersion":"v3.0.4"}', stderr="")
        assert command[1] == "verify"
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="no matching signatures: certificate identity or issuer mismatch",
        )

    monkeypatch.setattr("lean_runtime.publisher_verification.subprocess.run", run)
    verifier = CosignVerifier("wrong-identity", "wrong-issuer", executable=executable)
    with pytest.raises(EnvironmentError, match="signature verification failed.*identity or issuer"):
        verifier.verify(OCIRepository.parse("oci://ghcr.io/owner/cache"), "sha256:" + "a" * 64)


def test_cosign_attestation_binds_predicate_to_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "verification_tool"
    executable.write_text("")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='{"gitVersion":"v3.0.4"}' if command[1:3] == ["version", "--json"] else "",
            stderr="",
        )

    monkeypatch.setattr("lean_runtime.publisher_verification.subprocess.run", run)
    verifier = CosignVerifier(executable=executable)
    verifier.attest(
        OCIRepository.parse("oci://ghcr.io/owner/cache"),
        "sha256:" + "b" * 64,
        {"lock_id": "lock_123"},
    )
    command = commands[-1]
    assert command[1] == "attest"
    assert "https://lean-runtime.dev/attestation/environment/v1" in command
    assert command[-1] == "ghcr.io/owner/cache@sha256:" + "b" * 64
