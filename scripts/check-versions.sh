#!/usr/bin/env bash
# The version lives in four places. Assert the invariant between them.
#
#   pyproject == __init__ == Chart.appVersion   (all describe the CODE)
#   Chart.version >= Chart.appVersion           (chart may be ahead: a
#                                                chart-only fix bumps the chart
#                                                without rebuilding the image)
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
