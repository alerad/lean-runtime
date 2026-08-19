#!/usr/bin/env python3
"""Fail closed unless every catalog capsule and slim runtime is publicly usable.

The check is anonymous and validates all required platform indexes, media
types, blob availability, and byte-range support for sparse capsule packs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

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
    from lean_runtime.oci import CAPSULE_CONFIG_MEDIA_TYPE, capsule_reference
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
    platforms = [tuple(item.split("/", 1)) for item in arguments.platforms.split(",") if item]
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
            platforms,  # type: ignore[arg-type]
        )
        failures.extend(entry_failures)
        checked += 1
        print(("FAIL" if entry_failures else "ok  ") + f"  {entry.id}")
    if failures:
        print(f"\n{len(failures)} failure(s) across {checked} entries:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nall {checked} catalog entries have public sparse capsules and slim runtimes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
