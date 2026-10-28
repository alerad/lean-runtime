"""Detect uncataloged stable Mathlib releases and extend the discovery catalog.

`check` lists every stable `leanprover-community/mathlib4` tag newer than the
newest cataloged Mathlib entry (so a quick 4.34.0 → 4.34.1 sequence catalogs
both) and prints GitHub Actions output lines. `apply` appends the missing
manifest entries, then rebuilds the bundled catalog once, which resolves and
freezes each new exact lock under `catalog/locks/`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MATHLIB_TAGS_URL = "https://api.github.com/repos/leanprover-community/mathlib4/tags"
STABLE_TAG = re.compile(r"v4\.(\d+)\.(\d+)")
MANIFEST_ID = re.compile(r'^id = "([^"]+)"$', re.MULTILINE)
REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "catalog" / "environments.toml"
BUNDLED_CATALOG_PATH = REPO_ROOT / "lean_runtime" / "discovery" / "data" / "catalog.json"


def _github_rows(url: str) -> list[dict[str, object]]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "lean-runtime-catalog-update",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        rows = json.load(response)
    if not isinstance(rows, list):
        raise SystemExit(f"unexpected GitHub response shape from {url}")
    return rows


def _version(tag: str) -> tuple[int, int]:
    match = STABLE_TAG.fullmatch(tag)
    if match is None:
        raise SystemExit(f"not a stable mathlib4 tag: {tag!r}")
    return int(match.group(1)), int(match.group(2))


def stable_tags() -> list[str]:
    """Return every `vX.Y.Z` mathlib4 tag in version order."""
    tags: set[str] = set()
    for page in range(1, 6):
        rows = _github_rows(f"{MATHLIB_TAGS_URL}?per_page=100&page={page}")
        if not rows:
            break
        for row in rows:
            name = row.get("name")
            if isinstance(name, str) and STABLE_TAG.fullmatch(name) is not None:
                tags.add(name)
    if not tags:
        raise SystemExit("no stable mathlib4 tags found")
    return sorted(tags, key=_version)


def cataloged_ids() -> set[str]:
    return set(MANIFEST_ID.findall(MANIFEST_PATH.read_text(encoding="utf-8")))


def missing_tags(tags: list[str], cataloged: set[str]) -> list[str]:
    """Stable tags newer than the newest cataloged Mathlib release."""
    versions = [
        _version(entry.removeprefix("mathlib-"))
        for entry in cataloged
        if entry.startswith("mathlib-") and STABLE_TAG.fullmatch(entry.removeprefix("mathlib-"))
    ]
    newest = max(versions, default=(0, 0))
    return [tag for tag in tags if _version(tag) > newest]


def check() -> int:
    missing = missing_tags(stable_tags(), cataloged_ids())
    ids = [f"mathlib-{tag}" for tag in missing]
    print(f"tags={' '.join(missing)}")
    print(f"ids={' '.join(ids)}")
    print(f"missing={str(bool(missing)).lower()}")
    detail = ", ".join(missing) if missing else "none"
    print(f"uncataloged stable releases: {detail}", file=sys.stderr)
    return 0


def apply_tags(tags: list[str]) -> int:
    cataloged = cataloged_ids()
    for tag in tags:
        _version(tag)
        if f"mathlib-{tag}" in cataloged:
            raise SystemExit(f"catalog already contains mathlib-{tag}")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = MANIFEST_PATH.read_text(encoding="utf-8")
    manifest = re.sub(r'generated_at = "[^"]+"', f'generated_at = "{now}"', manifest, count=1)
    if not manifest.endswith("\n"):
        manifest += "\n"
    for tag in sorted(tags, key=_version):
        entry_id = f"mathlib-{tag}"
        manifest += (
            "\n[[environment]]\n"
            f'id = "{entry_id}"\n'
            'channel = "stable"\n'
            f'lock = "locks/{entry_id}.lock.json"\n'
            f'references = ["github:leanprover-community/mathlib4@{tag}"]\n'
            'inventory_packages = ["mathlib"]\n'
            f'created_at = "{now}"\n'
        )
    MANIFEST_PATH.write_text(manifest, encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "lean_runtime",
            "catalog",
            "build",
            str(MANIFEST_PATH),
            "--output",
            str(BUNDLED_CATALOG_PATH),
            "--previous",
            str(BUNDLED_CATALOG_PATH),
        ],
        check=True,
    )
    for tag in tags:
        lock_path = MANIFEST_PATH.parent / "locks" / f"mathlib-{tag}.lock.json"
        if not lock_path.is_file():
            raise SystemExit(f"catalog build did not freeze the expected lock: {lock_path}")
    print(f"added {', '.join(tags)}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="report uncataloged stable releases")
    apply_parser = commands.add_parser("apply", help="add releases and rebuild the catalog")
    apply_parser.add_argument(
        "--tag",
        required=True,
        action="append",
        help="stable mathlib4 tag such as v4.34.0 (repeatable)",
    )
    arguments = parser.parse_args(argv)
    if arguments.command == "check":
        return check()
    return apply_tags(arguments.tag)


if __name__ == "__main__":
    raise SystemExit(main())
