"""Explicit Sigstore/Cosign trust policy for OCI environment indexes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import EnvironmentError
from .oci import OCIRepository

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class CosignVerifier:
    def __init__(
        self,
        identity: str | None = None,
        issuer: str | None = None,
        *,
        executable: str | os.PathLike[str] = "cosign",
    ) -> None:
        if (identity is None) != (issuer is None):
            raise ValueError("Cosign verification requires both identity and OIDC issuer")
        resolved = shutil.which(str(executable))
        if resolved is None:
            candidate = Path(executable).expanduser()
            if not candidate.is_file():
                raise EnvironmentError(
                    "Cosign is required by the signature policy but not installed"
                )
            resolved = str(candidate)
        self.executable = resolved
        self.identity = identity
        self.issuer = issuer
        self._check_version()

    def _check_version(self) -> None:
        result = subprocess.run(
            [self.executable, "version", "--json"],
            text=True,
            capture_output=True,
            check=False,
        )
        version = ""
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
                if isinstance(payload, dict):
                    version = str(payload.get("gitVersion", payload.get("GitVersion", "")))
            except json.JSONDecodeError:
                version = result.stdout
        match = _VERSION.search(version)
        if match is None:
            raise EnvironmentError("could not determine the installed Cosign version")
        observed = tuple(int(part) for part in match.groups())
        if (
            observed[0] < 2
            or observed[0] == 2
            and observed < (2, 6, 2)
            or observed[0] == 3
            and observed < (3, 0, 4)
        ):
            raise EnvironmentError("Cosign 2.6.2 or 3.0.4+ is required for secure verification")

    @staticmethod
    def _subject(repository: OCIRepository, digest: str) -> str:
        return f"{repository.registry}/{repository.repository}@{digest}"

    def verify(self, repository: OCIRepository, digest: str) -> None:
        if self.identity is None or self.issuer is None:
            raise EnvironmentError("Cosign verifier has no trusted publisher identity")
        command = [
            self.executable,
            "verify",
            "--certificate-identity",
            self.identity,
            "--certificate-oidc-issuer",
            self.issuer,
        ]
        if repository.insecure:
            command.append("--allow-http-registry")
        self._registry_credentials(command)
        command.append(self._subject(repository, digest))
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode:
            raise EnvironmentError(
                "prebuilt environment signature verification failed: "
                + (result.stdout + result.stderr)[-2000:]
            )

    def sign(self, repository: OCIRepository, digest: str) -> None:
        command = [self.executable, "sign", "--yes"]
        if repository.insecure:
            command.append("--allow-http-registry")
        self._registry_credentials(command)
        command.append(self._subject(repository, digest))
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode:
            raise EnvironmentError(
                "prebuilt environment signing failed: " + (result.stdout + result.stderr)[-2000:]
            )

    def attest(self, repository: OCIRepository, digest: str, predicate: dict[str, object]) -> None:
        with tempfile.TemporaryDirectory(prefix="lean-runtime-attest-") as temporary:
            path = Path(temporary) / "predicate.json"
            path.write_text(json.dumps(predicate, sort_keys=True), encoding="utf-8")
            command = [
                self.executable,
                "attest",
                "--yes",
                "--predicate",
                str(path),
                "--type",
                "https://lean-runtime.dev/attestation/environment/v1",
            ]
            if repository.insecure:
                command.append("--allow-http-registry")
            self._registry_credentials(command)
            command.append(self._subject(repository, digest))
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            if result.returncode:
                raise EnvironmentError(
                    "prebuilt environment attestation failed: "
                    + (result.stdout + result.stderr)[-2000:]
                )

    @staticmethod
    def _registry_credentials(command: list[str]) -> None:
        username = os.environ.get("LEAN_RUNTIME_REGISTRY_USERNAME")
        password = os.environ.get("LEAN_RUNTIME_REGISTRY_PASSWORD")
        if username is not None and password is not None:
            command.extend(("--registry-username", username, "--registry-password", password))
