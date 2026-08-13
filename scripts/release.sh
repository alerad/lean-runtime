#!/usr/bin/env bash
# One-command release: bump, verify, tag, and publish atomically so the tag
# can never disagree with pyproject (the release workflow's version guard).
set -euo pipefail
VERSION="${1:?usage: scripts/release.sh X.Y.Z}"
[[ "$(git branch --show-current)" == "main" ]] || { echo "run on main"; exit 1; }
git pull --ff-only
grep -q "^version = \"$VERSION\"$" pyproject.toml || {
  sed -i.bak "s/^version = \".*\"$/version = \"$VERSION\"/" pyproject.toml && rm pyproject.toml.bak
}
grep -q "^## $VERSION - " CHANGELOG.md || {
  DATE=$(date +%Y-%m-%d)
  sed -i.bak "s/^## Unreleased$/## $VERSION - $DATE/" CHANGELOG.md && rm CHANGELOG.md.bak
}
grep -q "^## $VERSION - " CHANGELOG.md || { echo "CHANGELOG has no $VERSION section"; exit 1; }
python -m pytest -q
git add pyproject.toml CHANGELOG.md
git diff --cached --quiet || git commit -m "chore: release $VERSION"
git push origin main
NOTES=$(awk "/^## $VERSION - /{flag=1;next}/^## /{flag=0}flag" CHANGELOG.md)
gh release create "v$VERSION" --target "$(git rev-parse HEAD)" --title "v$VERSION" --notes "$NOTES"
echo "released v$VERSION"
