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

release_work="$(mktemp -d)"
release_commit_ready=false
cleanup() {
  rm -rf "$release_work"
  if [[ "$release_commit_ready" != true ]]; then
    git restore --staged -- pyproject.toml CHANGELOG.md 2>/dev/null || true
    git restore -- pyproject.toml CHANGELOG.md
  fi
}
trap cleanup EXIT
python -m venv "$release_work/venv"
release_python="$release_work/venv/bin/python"
"$release_python" -m pip install --quiet --upgrade pip
"$release_python" -m pip install --quiet -e '.[dev,docs]' build twine

RELEASE_VERSION="$VERSION" "$release_python" - <<'PY'
import datetime
import os
import re
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
if re.search(rf"^## {re.escape(version)} - \d{{4}}-\d{{2}}-\d{{2}}$", text, re.M) is None:
    text = text.replace(heading, f"{heading}\n\n{dated}", 1)
changelog.write_text(text)
PY

"$release_python" -m pip install --quiet --no-deps -e .
"$release_python" -m ruff check .
"$release_python" -m ruff format --check .
"$release_python" -m mypy --strict lean_runtime
"$release_python" -m pytest
"$release_python" -m mkdocs build --strict
release_dist="$release_work/dist"
mkdir "$release_dist"
"$release_python" -m build --outdir "$release_dist"
"$release_python" -m twine check "$release_dist"/*
"$release_python" scripts/smoke_wheel.py "$release_dist"/*.whl

if ! git diff --quiet -- pyproject.toml CHANGELOG.md; then
  git add pyproject.toml CHANGELOG.md
  git commit -m "chore: release $VERSION"
fi
release_sha="$(git rev-parse HEAD)"
release_commit_ready=true
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
