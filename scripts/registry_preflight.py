#!/usr/bin/env python3
"""Fail closed unless every catalog capsule and slim runtime is publicly usable.

The check is anonymous and validates all required platform indexes, media
types, blob availability, and byte-range support for sparse capsule packs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ACCEPT = "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json"
DEFAULT_PLATFORMS = "linux/amd64,darwin/arm64,darwin/amd64"
TIMEOUT = 30


def anonymous_token(registry: str, repository: str) -> str:
    query = urllib.parse.urlencode({"service": registry, "scope": f"repository:{repository}:pull"})
    with urllib.request.urlopen(f"https://{registry}/token?{query}", timeout=TIMEOUT) as response:
        return str(json.loads(response.read())["token"])


def _get(
    url: str,
    token: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", ACCEPT)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                data = response.read() if method == "GET" else b""
                return response.status, data, dict(response.headers)
        except urllib.error.HTTPError as error:
            if error.code not in {408, 429} and not 500 <= error.code < 600:
                return error.code, b"", dict(error.headers)
            failure = f"HTTP {error.code}"
        except (OSError, urllib.error.URLError) as error:
            failure = str(error)
        if attempt < 3:
            time.sleep(0.25 * attempt)
    return 0, b"", {"X-Preflight-Error": failure}


def _failure(status: int, headers: dict[str, str]) -> str:
    if status:
        return f"HTTP {status}"
    return "transport error: " + headers.get("X-Preflight-Error", "unknown failure")


def check_entry(
    registry: str,
    repository: str,
    token: str,
    entry_id: str,
    lock_id: str,
    toolchain: str,
    platforms: list[tuple[str, str]],
) -> list[str]:
    """Return failures for one capsule and its exact check-only toolchain."""
    from lean_runtime.bundles import CAPSULE_CONFIG_MEDIA_TYPE
    from lean_runtime.oci import capsule_reference
    from lean_runtime.packs import PACK_MEDIA_TYPE
    from lean_runtime.toolchain_oci import (
        TOOLCHAIN_CONFIG_MEDIA_TYPE,
        TOOLCHAIN_LAYER_MEDIA_TYPE,
        toolchain_reference,
    )

    base = f"https://{registry}/v2/{repository}"
    failures: list[str] = []
    references = (
        ("capsule", capsule_reference(lock_id), CAPSULE_CONFIG_MEDIA_TYPE, PACK_MEDIA_TYPE, True),
        (
            "toolchain",
            toolchain_reference(toolchain),
            TOOLCHAIN_CONFIG_MEDIA_TYPE,
            TOOLCHAIN_LAYER_MEDIA_TYPE,
            False,
        ),
    )
    for kind, reference, config_type, layer_type, require_range in references:
        status, data, headers = _get(f"{base}/manifests/{reference}", token)
        if status != 200:
            failures.append(f"{entry_id}: {kind} index {reference} -> {_failure(status, headers)}")
            continue
        manifests = json.loads(data).get("manifests")
        if not isinstance(manifests, list):
            failures.append(f"{entry_id}: {kind} reference is not a multi-platform index")
            continue
        for wanted_os, wanted_arch in platforms:
            descriptor = next(
                (
                    item
                    for item in manifests
                    if isinstance(item.get("platform"), dict)
                    and item["platform"].get("os") == wanted_os
                    and item["platform"].get("architecture") == wanted_arch
                ),
                None,
            )
            label = f"{entry_id}: {kind} {wanted_os}/{wanted_arch}"
            if descriptor is None:
                failures.append(f"{label} manifest is missing")
                continue
            status, data, headers = _get(f"{base}/manifests/{descriptor['digest']}", token)
            if status != 200:
                failures.append(f"{label} manifest -> {_failure(status, headers)}")
                continue
            platform_manifest = json.loads(data)
            config = platform_manifest.get("config", {})
            layers = platform_manifest.get("layers", [])
            if config.get("mediaType") != config_type or not isinstance(layers, list):
                failures.append(f"{label} config media type mismatch")
                continue
            if any(
                not isinstance(layer, dict) or layer.get("mediaType") != layer_type
                for layer in layers
            ):
                failures.append(f"{label} layer media type mismatch")
                continue
            for blob in [config, *layers]:
                digest = blob.get("digest")
                if not isinstance(digest, str):
                    failures.append(f"{label} descriptor has no digest")
                    continue
                status, _, headers = _get(f"{base}/blobs/{digest}", token, method="HEAD")
                if status != 200:
                    failures.append(f"{label} blob {digest[:19]} -> {_failure(status, headers)}")
            if require_range and layers:
                digest = layers[0]["digest"]
                status, ranged, headers = _get(
                    f"{base}/blobs/{digest}", token, headers={"Range": "bytes=0-0"}
                )
                content_range = headers.get("Content-Range", headers.get("content-range", ""))
                if status != 206 or len(ranged) != 1 or not content_range.startswith("bytes 0-0/"):
                    failures.append(f"{label} pack does not support byte ranges")
    return failures


def check_declaration_index(
    registry: str,
    repository: str,
    token: str,
    entry_id: str,
    lock_id: str,
) -> list[str]:
    """Verify public shard metadata and materialize the smallest shard anonymously."""

    import zstandard

    from lean_runtime.declaration_index import (
        DECLARATION_INDEX_SCHEMA,
        DeclarationIndex,
        DeclarationShard,
    )
    from lean_runtime.declaration_index_oci import (
        DECLARATION_INDEX_CONFIG_MEDIA_TYPE,
        DECLARATION_INDEX_LAYER_MEDIA_TYPE,
        declaration_index_reference,
    )

    failures: list[str] = []
    base = f"https://{registry}/v2/{repository}"
    reference = declaration_index_reference(lock_id)
    status, manifest_data, headers = _get(f"{base}/manifests/{reference}", token)
    label = f"{entry_id}: declaration index"
    if status != 200:
        return [f"{label} {reference} -> {_failure(status, headers)}"]
    try:
        manifest = json.loads(manifest_data)
        config_descriptor = manifest["config"]
        layer_descriptors = manifest["layers"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return [f"{label} manifest is malformed"]
    if (
        not isinstance(config_descriptor, dict)
        or config_descriptor.get("mediaType") != DECLARATION_INDEX_CONFIG_MEDIA_TYPE
        or not isinstance(layer_descriptors, list)
        or not layer_descriptors
        or any(
            not isinstance(item, dict)
            or item.get("mediaType") != DECLARATION_INDEX_LAYER_MEDIA_TYPE
            for item in layer_descriptors
        )
    ):
        return [f"{label} manifest media types are invalid"]
    config_digest = config_descriptor.get("digest")
    if not isinstance(config_digest, str):
        return [f"{label} config descriptor has no digest"]
    status, config_data, headers = _get(f"{base}/blobs/{config_digest}", token)
    if status != 200:
        return [f"{label} config -> {_failure(status, headers)}"]
    if "sha256:" + hashlib.sha256(config_data).hexdigest() != config_digest:
        return [f"{label} config digest mismatch"]
    try:
        config = json.loads(config_data)
        raw_shards = config["shards"]
        if not isinstance(raw_shards, list) or not all(
            isinstance(item, dict) for item in raw_shards
        ):
            raise TypeError
        shards = tuple(DeclarationShard.from_dict(item) for item in raw_shards)
    except (KeyError, TypeError, json.JSONDecodeError, ValueError):
        return [f"{label} config is malformed"]
    if config.get("schema") != DECLARATION_INDEX_SCHEMA or config.get("lock_id") != lock_id:
        return [f"{label} config identity mismatch"]
    descriptors = {
        str(item.get("digest")): item for item in layer_descriptors if isinstance(item, dict)
    }
    if len(shards) != len(descriptors) or any(
        item.layer_digest not in descriptors
        or descriptors[item.layer_digest].get("size") != item.layer_size
        for item in shards
    ):
        return [f"{label} shard descriptors do not match the config"]
    for shard in shards:
        status, _, headers = _get(f"{base}/blobs/{shard.layer_digest}", token, method="HEAD")
        if status != 200:
            failures.append(f"{label} shard {shard.package} -> {_failure(status, headers)}")
    if failures:
        return failures
    sample = min(shards, key=lambda item: item.layer_size)
    status, compressed, headers = _get(f"{base}/blobs/{sample.layer_digest}", token)
    if status != 200:
        return [f"{label} sample shard -> {_failure(status, headers)}"]
    if "sha256:" + hashlib.sha256(compressed).hexdigest() != sample.layer_digest:
        return [f"{label} sample layer digest mismatch"]
    try:
        sqlite_data = zstandard.ZstdDecompressor().decompress(
            compressed, max_output_size=sample.sqlite_size
        )
    except zstandard.ZstdError:
        return [f"{label} sample layer is not valid zstd"]
    if (
        len(sqlite_data) != sample.sqlite_size
        or "sha256:" + hashlib.sha256(sqlite_data).hexdigest() != sample.sqlite_digest
    ):
        return [f"{label} sample SQLite digest mismatch"]
    with tempfile.TemporaryDirectory(prefix="lean-runtime-index-preflight-") as temporary:
        path = Path(temporary) / "sample.sqlite"
        path.write_bytes(sqlite_data)
        try:
            index = DeclarationIndex(path, expected_shard_id=sample.shard_id)
            if index.declaration_count < 1:
                return [f"{label} sample shard contains no declarations"]
        except Exception as error:  # defensive public-artifact boundary
            return [f"{label} sample SQLite is invalid: {error}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platforms", default=DEFAULT_PLATFORMS)
    parser.add_argument("--skip", action="append", default=[], help="catalog entry id to skip")
    parser.add_argument("--library", default=None, help="override oci://HOST/REPOSITORY")
    arguments = parser.parse_args()

    from lean_runtime.discovery import default_catalog
    from lean_runtime.oci import DEFAULT_ENVIRONMENT_LIBRARIES

    reference = (arguments.library or DEFAULT_ENVIRONMENT_LIBRARIES[0]).removeprefix("oci://")
    registry, _, repository = reference.partition("/")
    platforms: list[tuple[str, str]] = []
    for item in arguments.platforms.split(","):
        operating_system, separator, architecture = item.partition("/")
        if not separator or not operating_system or not architecture:
            parser.error(f"invalid platform: {item!r}")
        platforms.append((operating_system, architecture))
    try:
        token = anonymous_token(registry, repository)
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as error:
        print(f"FAIL: anonymous pull token for {registry}/{repository}: {error}")
        return 1

    failures: list[str] = []
    checked = 0
    for entry in default_catalog().entries:
        if entry.id in arguments.skip:
            print(f"skip  {entry.id}")
            continue
        entry_failures = check_entry(
            registry,
            repository,
            token,
            entry.id,
            entry.lock.lock_id,
            entry.toolchain,
            platforms,
        )
        entry_failures.extend(
            check_declaration_index(
                registry,
                repository,
                token,
                entry.id,
                entry.lock.lock_id,
            )
        )
        failures.extend(entry_failures)
        checked += 1
        print(("FAIL" if entry_failures else "ok  ") + f"  {entry.id}")
    if failures:
        print(f"\n{len(failures)} failure(s) across {checked} entries:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        f"\nall {checked} catalog entries have public sparse capsules, slim runtimes, "
        "and declaration shards"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
