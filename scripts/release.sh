#!/usr/bin/env bash
# Cut a metalnap release.
#
#   ./scripts/release.sh app   0.3.0    # code changed: bumps everything
#   ./scripts/release.sh chart 0.2.8    # chart only: appVersion stays put
#
# The version lives in four places and they drift the moment anyone bumps them
# by hand -- image 0.2.4 shipped with __version__ = "0.2.3" because of exactly
# that. This is the only supported way to bump them.
#
# Two release kinds, because they are genuinely different:
#
#   app    the Python changed. Chart version AND appVersion move together, and
#          a new image is built from this code.
#   chart  only templates/values/docs changed. The chart version moves; the
#          appVersion does NOT, because the image it points at is unchanged.
set -euo pipefail
cd "$(dirname "$0")/.."

KIND="${1:-}"; VERSION="${2:-}"
case "$KIND" in app|chart) ;; *) echo "usage: $0 app|chart <version>"; exit 1;; esac
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z, got '$VERSION'"; exit 1; }

git diff --quiet && git diff --cached --quiet || { echo "❌ working tree is dirty"; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { echo "❌ not on main"; exit 1; }
git tag -l "v$VERSION" | grep -q . && { echo "❌ tag v$VERSION already exists"; exit 1; }

echo "==> bumping to $VERSION ($KIND release)"
sed -i.bak -E "s/^version: .*/version: $VERSION/" charts/metalnap/Chart.yaml
if [ "$KIND" = app ]; then
  sed -i.bak -E "s/^appVersion: .*/appVersion: \"$VERSION\"/" charts/metalnap/Chart.yaml
  sed -i.bak -E "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
  sed -i.bak -E "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" metalnap/__init__.py
fi
rm -f charts/metalnap/Chart.yaml.bak pyproject.toml.bak metalnap/__init__.py.bak

./scripts/check-versions.sh

grep -q "^## .*$VERSION" CHANGELOG.md || {
  echo "❌ CHANGELOG.md has no entry for $VERSION."
  echo "   Write it first -- a release with no notes is a release nobody can"
  echo "   evaluate, and the tag message becomes the GitHub release body."
  git checkout -- charts/metalnap/Chart.yaml pyproject.toml metalnap/__init__.py
  exit 1
}

echo "==> gates"
python3 charts/validate.py charts/metalnap/Chart.yaml
helm lint charts/metalnap --set 'nodes={a,b}' >/dev/null && echo "  helm lint ok"
python3 -B tests/test_controller.py 2>&1 | tail -1
python3 -B tests/sim.py --seeds 60 --ticks 900 | tail -1

NOTES=$(awk "/^## .*$VERSION/{f=1;next} /^## /{f=0} f" CHANGELOG.md)
git commit -qam "release $VERSION ($KIND)"
git tag -a "v$VERSION" -m "metalnap v$VERSION

$NOTES"
echo
echo "==> ready. Push to publish (this triggers image, chart and GitHub release):"
echo "     git push origin main && git push origin v$VERSION"
