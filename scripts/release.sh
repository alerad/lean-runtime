#!/usr/bin/env bash
# Prepare a release commit, wait for that exact commit's CI, then push its tag.
# The tag workflow builds the wheel again, publishes PyPI, and only then creates
# the GitHub release. A failed stage is safely rerunnable and cannot publish a
# release from a different commit.
set -euo pipefail

VERSION="${1:?usage: scripts/release.sh X.Y.Z}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid version: $VERSION"; exit 1; }
[[ "$(git branch --show-current)" == "main" ]] || { echo "run on main"; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo "working tree is not clean"; exit 1; }
git pull --ff-only

if git rev-parse "v$VERSION" >/dev/null 2>&1; then
  echo "tag v$VERSION already exists"
  exit 1
fi

RELEASE_VERSION="$VERSION" python - <<'PY'
import datetime
import os
from pathlib import Path

version = os.environ["RELEASE_VERSION"]
project = Path("pyproject.toml")
text = project.read_text()
lines = text.splitlines(keepends=True)
for index, line in enumerate(lines):
    if line.startswith("version = "):
        lines[index] = f'version = "{version}"\n'
        break
else:
    raise SystemExit("pyproject.toml has no project version")
project.write_text("".join(lines))

changelog = Path("CHANGELOG.md")
text = changelog.read_text()
heading = "## Unreleased"
if heading not in text:
    raise SystemExit("CHANGELOG.md has no Unreleased section")
dated = f"## {version} - {datetime.date.today().isoformat()}"
changelog.write_text(text.replace(heading, f"{heading}\n\n{dated}", 1))
PY

python -m ruff check .
python -m ruff format --check .
python -m mypy --strict lean_runtime
python -m pytest
python -m mkdocs build --strict
release_dist="$(mktemp -d)"
trap 'rm -rf "$release_dist"' EXIT
python -m build --outdir "$release_dist"
python -m twine check "$release_dist"/*
python scripts/smoke_wheel.py "$release_dist"/*.whl

git add pyproject.toml CHANGELOG.md
git commit -m "chore: release $VERSION"
release_sha="$(git rev-parse HEAD)"
git push origin main

run_id=""
for _attempt in {1..60}; do
  run_id="$(gh run list --workflow ci.yml --commit "$release_sha" --limit 1 --json databaseId --jq '.[0].databaseId // empty')"
  [[ -n "$run_id" ]] && break
  sleep 5
done
[[ -n "$run_id" ]] || { echo "CI did not start for $release_sha"; exit 1; }
gh run watch "$run_id" --exit-status

git tag -a "v$VERSION" "$release_sha" -m "v$VERSION"
git push origin "v$VERSION"
echo "pushed v$VERSION; the release workflow will publish PyPI, then GitHub"
