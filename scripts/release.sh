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
cp pyproject.toml "$release_work/pyproject.toml.before"
cp CHANGELOG.md "$release_work/CHANGELOG.md.before"
cleanup() {
  if [[ "$release_commit_ready" != true ]]; then
    cp "$release_work/pyproject.toml.before" pyproject.toml
    cp "$release_work/CHANGELOG.md.before" CHANGELOG.md
  fi
  rm -rf "$release_work"
}
trap cleanup EXIT
python -m venv "$release_work/venv"
release_python="$release_work/venv/bin/python"
"$release_python" -m pip install --quiet --upgrade pip
"$release_python" -m pip install --quiet -e '.[dev,docs]' build twine

"$release_python" -m ruff check .
"$release_python" -m ruff format --check .
"$release_python" -m mypy --strict --cache-dir "$release_work/mypy-cache" lean_runtime
"$release_python" -m pytest
"$release_python" -m mkdocs build --strict

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
echo "waiting for CI to appear for $release_sha"
for attempt in {1..240}; do
  run_id="$(gh run list --workflow ci.yml --commit "$release_sha" --limit 1 --json databaseId --jq '.[0].databaseId // empty')"
  [[ -n "$run_id" ]] && break
  if (( attempt == 13 )); then
    remote_sha="$(git ls-remote origin refs/heads/main | awk '{print $1}')"
    [[ "$remote_sha" == "$release_sha" ]] || {
      echo "origin/main moved to $remote_sha while waiting for release CI"
      exit 1
    }
    echo "push-triggered CI has not appeared; dispatching CI for origin/main"
    gh workflow run ci.yml --ref main
  fi
  if (( attempt % 12 == 0 )); then
    echo "still waiting for CI (${attempt} checks, $((attempt * 5))s)"
  fi
  sleep 5
done
[[ -n "$run_id" ]] || { echo "CI did not start for $release_sha"; exit 1; }
gh run watch "$run_id" --exit-status

git tag -a "v$VERSION" "$release_sha" -m "v$VERSION"
git push origin "v$VERSION"
echo "pushed v$VERSION; the release workflow will publish PyPI, then GitHub"
