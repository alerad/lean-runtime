"""Shared, format-stable primitives for lean-runtime OCI artifacts.

Environment capsules, slim toolchains, and ready programs have different
payload schemas but use the same OCI descriptor and platform contracts.  This
module owns that common wire vocabulary; payload modules must not import one
another's private protocol helpers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import EnvironmentError
from .store import platform_compatibility

MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def blob_descriptor(data: bytes, media_type: str, **extra: Any) -> dict[str, Any]:
    return {"mediaType": media_type, "digest": digest_bytes(data), "size": len(data), **extra}


def blob_descriptor_path(path: Path, media_type: str, **extra: Any) -> dict[str, Any]:
    return {
        "mediaType": media_type,
        "digest": digest_path(path),
        "size": path.stat().st_size,
        **extra,
    }


def json_object(data: bytes, label: str, *, subject: str = "bundle") -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvironmentError(f"{subject} {label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise EnvironmentError(f"{subject} {label} must be a JSON object")
    return value


def descriptor_blob(
    entries: Mapping[str, bytes],
    descriptor: Mapping[str, Any],
    label: str,
) -> bytes:
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise EnvironmentError(f"bundle {label} has an invalid digest")
    data = entries.get("blobs/sha256/" + digest.removeprefix("sha256:"))
    if data is None or len(data) != size or digest_bytes(data) != digest:
        raise EnvironmentError(f"bundle {label} digest mismatch")
    return data


def descriptor_blob_path(
    entries: Mapping[str, Path],
    descriptor: Mapping[str, Any],
    label: str,
) -> Path:
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise EnvironmentError(f"bundle {label} has an invalid digest")
    path = entries.get("blobs/sha256/" + digest.removeprefix("sha256:"))
    if path is None or path.stat().st_size != size or digest_path(path) != digest:
        raise EnvironmentError(f"bundle {label} digest mismatch")
    return path


def require_media_type(descriptor: Mapping[str, Any], expected: str, label: str) -> None:
    if descriptor.get("mediaType") != expected:
        raise EnvironmentError(f"bundle {label} has an unsupported media type")


def platform_matches(
    descriptor: Mapping[str, Any],
    *,
    artifact_kind: str | None = None,
) -> bool:
    """Match the host ABI encoded in a multi-platform OCI descriptor."""
    platform = descriptor.get("platform")
    annotations = descriptor.get("annotations")
    compatibility = platform_compatibility()
    architecture = {"x86_64": "amd64", "arm64": "arm64"}.get(
        compatibility["machine"], compatibility["machine"]
    )
    return (
        isinstance(platform, dict)
        and isinstance(annotations, dict)
        and platform.get("os") == compatibility["system"]
        and platform.get("architecture") == architecture
        and annotations.get("org.lean-runtime.platform.abi") == compatibility["abi"]
        and (
            artifact_kind is None
            or annotations.get("org.lean-runtime.artifact.kind") == artifact_kind
        )
    )
