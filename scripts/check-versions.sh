#!/usr/bin/env bash
# The version lives in four places. Assert the invariant between them.
#
#   pyproject == __init__ == Chart.appVersion == Chart.version
#
# release-please bumps all four together, so they are now always equal. This
# check is not therefore redundant: it is what catches a release-please
# misconfiguration (a missing extra-file, a renamed annotation) before a
# release goes out carrying a version that only three of the four files agree
# on. That drift shipped once already -- image 0.2.4 with __version__ 0.2.3.
set -euo pipefail
cd "$(dirname "$0")/.."

CV=$(grep '^version:'    charts/metalnap/Chart.yaml | awk '{print $2}')
AV=$(grep '^appVersion:' charts/metalnap/Chart.yaml | awk '{print $2}' | tr -d '"')
PV=$(grep '^version'     pyproject.toml             | awk -F'"' '{print $2}')
IV=$(grep '__version__'  metalnap/__init__.py       | awk -F'"' '{print $2}')

fail=0
if [ "$PV" != "$AV" ] || [ "$IV" != "$AV" ]; then
  echo "❌ code versions disagree with appVersion:"
  echo "     Chart appVersion : $AV"
  echo "     pyproject.toml   : $PV"
  echo "     __init__.py      : $IV"
  echo "   These all describe the same image and must match."
  fail=1
fi
if [ "$(printf '%s\n%s\n' "$AV" "$CV" | sort -V | tail -1)" != "$CV" ]; then
  echo "❌ chart version ($CV) is behind appVersion ($AV)"
  fail=1
fi
[ "$fail" = 0 ] && echo "  versions consistent: chart=$CV appVersion=$AV code=$PV"
exit $fail
