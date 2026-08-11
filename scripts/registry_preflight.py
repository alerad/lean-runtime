#!/usr/bin/env python3
"""Verify every bundled catalog entry is anonymously downloadable.

For each catalog lock this checks, without any registry credentials:

1. the anonymous GHCR pull token can be obtained;
2. the OCI index tagged with the lock id exists;
3. a platform manifest exists for every required platform;
4. every blob in each required platform manifest answers a HEAD request.

A catalog entry is a public claim that a prebuilt environment can be
downloaded. This script fails when any claim lacks an externally retrievable
artifact, which is exactly the failure mode that otherwise degrades into a
silent half-hour source build on end-user machines.

Usage:
    python scripts/registry_preflight.py
    python scripts/registry_preflight.py --platforms linux/amd64,darwin/arm64
    python scripts/registry_preflight.py --skip core-v4.32.2
"""

from __future__ import annotations

import argparse
import json
import sys
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


def _get(url: str, token: str, *, method: str = "GET") -> tuple[int, bytes]:
    request = urllib.request.Request(url, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", ACCEPT)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read() if method == "GET" else b""
    except urllib.error.HTTPError as error:
        return error.code, b""


def check_entry(
    registry: str,
    repository: str,
    token: str,
    entry_id: str,
    lock_id: str,
    platforms: list[tuple[str, str]],
) -> list[str]:
    """Return a list of human-readable failures for one catalog entry."""
    base = f"https://{registry}/v2/{repository}"
    status, data = _get(f"{base}/manifests/{lock_id}", token)
    if status != 200:
        return [f"{entry_id}: index manifest {lock_id} -> HTTP {status}"]
    document = json.loads(data)
    manifests = document.get("manifests")
    if not isinstance(manifests, list):
        # A single-platform manifest published without an index.
        manifests = [{"digest": None, "platform": None, "_flat": document}]
    failures: list[str] = []
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
        if descriptor is None:
            failures.append(f"{entry_id}: no platform manifest for {wanted_os}/{wanted_arch}")
            continue
        status, data = _get(f"{base}/manifests/{descriptor['digest']}", token)
        if status != 200:
            failures.append(f"{entry_id}: {wanted_os}/{wanted_arch} manifest -> HTTP {status}")
            continue
        platform_manifest = json.loads(data)
        blobs = [platform_manifest.get("config", {})] + list(platform_manifest.get("layers", []))
        for blob in blobs:
            digest = blob.get("digest")
            if not digest:
                continue
            status, _ = _get(f"{base}/blobs/{digest}", token, method="HEAD")
            if status != 200:
                failures.append(
                    f"{entry_id}: {wanted_os}/{wanted_arch} blob {digest[:19]} -> HTTP {status}"
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platforms", default=DEFAULT_PLATFORMS)
    parser.add_argument("--skip", action="append", default=[], help="catalog entry id to skip")
    parser.add_argument("--library", default=None, help="override oci://HOST/REPOSITORY")
    arguments = parser.parse_args()

    from lean_runtime.discovery import default_catalog
    from lean_runtime.oci import DEFAULT_ENVIRONMENT_LIBRARIES

    library = arguments.library or DEFAULT_ENVIRONMENT_LIBRARIES[0]
    reference = library.removeprefix("oci://")
    registry, _, repository = reference.partition("/")
    platforms = [
        (item.split("/")[0], item.split("/")[1]) for item in arguments.platforms.split(",") if item
    ]

    try:
        token = anonymous_token(registry, repository)
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as error:
        print(
            f"FAIL: anonymous pull token for {registry}/{repository} is unavailable: "
            f"{error}\n      (is the GHCR package public?)"
        )
        return 1

    catalog = default_catalog()
    failures: list[str] = []
    checked = 0
    for entry in catalog.entries:
        if entry.id in arguments.skip:
            print(f"skip  {entry.id}")
            continue
        entry_failures = check_entry(
            registry, repository, token, entry.id, entry.lock.lock_id, platforms
        )
        checked += 1
        if entry_failures:
            failures.extend(entry_failures)
            print(f"FAIL  {entry.id}")
        else:
            print(f"ok    {entry.id}")
    if failures:
        print(f"\n{len(failures)} failure(s) across {checked} entries:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nall {checked} catalog entries are anonymously downloadable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
