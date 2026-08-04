from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lean_runtime import EnvironmentError
from lean_runtime.oci import OCIRepository
from lean_runtime.signatures import CosignVerifier


def test_cosign_verification_binds_digest_identity_and_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "cosign"
    executable.write_text("")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command[1:3] == ["version", "--json"]:
            return SimpleNamespace(returncode=0, stdout='{"gitVersion":"v3.0.4"}', stderr="")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("lean_runtime.signatures.subprocess.run", run)
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
    executable = tmp_path / "cosign"
    executable.write_text("")
    monkeypatch.setattr(
        "lean_runtime.signatures.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout='{"gitVersion":"v3.0.3"}', stderr=""
        ),
    )
    with pytest.raises(EnvironmentError, match="3.0.4"):
        CosignVerifier("identity", "issuer", executable=executable)


def test_cosign_attestation_binds_predicate_to_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "cosign"
    executable.write_text("")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='{"gitVersion":"v3.0.4"}' if command[1:3] == ["version", "--json"] else "",
            stderr="",
        )

    monkeypatch.setattr("lean_runtime.signatures.subprocess.run", run)
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
