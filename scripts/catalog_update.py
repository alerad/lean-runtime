"""Detect the newest stable Mathlib release and extend the discovery catalog.

`check` compares the newest stable `leanprover-community/mathlib4` tag against
`catalog/environments.toml` and prints GitHub Actions output lines. `apply`
appends the missing manifest entry, then rebuilds the bundled catalog, which
resolves and freezes the new exact lock under `catalog/locks/`.
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


def newest_stable_tag() -> str:
    """Return the newest `vX.Y.Z` mathlib4 tag by version order."""
    best: tuple[tuple[int, int], str] | None = None
    for page in range(1, 6):
        rows = _github_rows(f"{MATHLIB_TAGS_URL}?per_page=100&page={page}")
        if not rows:
            break
        for row in rows:
            name = row.get("name")
            if not isinstance(name, str):
                continue
            match = STABLE_TAG.fullmatch(name)
            if match is None:
                continue
            key = (int(match.group(1)), int(match.group(2)))
            if best is None or key > best[0]:
                best = (key, name)
    if best is None:
        raise SystemExit("no stable mathlib4 tags found")
    return best[1]


def cataloged_ids() -> set[str]:
    return set(MANIFEST_ID.findall(MANIFEST_PATH.read_text(encoding="utf-8")))


def check() -> int:
    tag = newest_stable_tag()
    entry_id = f"mathlib-{tag}"
    missing = entry_id not in cataloged_ids()
    print(f"tag={tag}")
    print(f"id={entry_id}")
    print(f"missing={str(missing).lower()}")
    state = "missing from" if missing else "already in"
    print(f"newest stable release {tag} is {state} the catalog", file=sys.stderr)
    return 0


def apply_tag(tag: str) -> int:
    if STABLE_TAG.fullmatch(tag) is None:
        raise SystemExit(f"not a stable mathlib4 tag: {tag!r}")
    entry_id = f"mathlib-{tag}"
    if entry_id in cataloged_ids():
        raise SystemExit(f"catalog already contains {entry_id}")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = MANIFEST_PATH.read_text(encoding="utf-8")
    manifest = re.sub(r'generated_at = "[^"]+"', f'generated_at = "{now}"', manifest, count=1)
    if not manifest.endswith("\n"):
        manifest += "\n"
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
    lock_path = MANIFEST_PATH.parent / "locks" / f"{entry_id}.lock.json"
    if not lock_path.is_file():
        raise SystemExit(f"catalog build did not freeze the expected lock: {lock_path}")
    print(f"added {entry_id}; lock frozen at {lock_path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="report the newest stable release and catalog state")
    apply_parser = commands.add_parser("apply", help="add one release and rebuild the catalog")
    apply_parser.add_argument("--tag", required=True, help="stable mathlib4 tag such as v4.34.0")
    arguments = parser.parse_args(argv)
    if arguments.command == "check":
        return check()
    return apply_tag(arguments.tag)


if __name__ == "__main__":
    raise SystemExit(main())
