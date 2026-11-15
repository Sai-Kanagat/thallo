#!/usr/bin/env bash
# The web app's checks (WP-09): type check, lint, component tests, production build.
# Kept apart from scripts/ci.sh because `npm ci` needs the network to fetch packages the first
# time; once web/node_modules exists everything below runs offline. No telemetry, no fonts or
# scripts fetched at build time.
#
#   scripts/ci_web.sh            install if needed, then check
#   SKIP_BUILD=1 scripts/ci_web.sh
set -euo pipefail
cd "$(dirname "$0")/../web"
export NEXT_TELEMETRY_DISABLED=1

[ -d node_modules ] || npm ci --no-audit --no-fund

echo "1/4 type check"
./node_modules/.bin/tsc --noEmit
echo "2/4 lint"
./node_modules/.bin/eslint --max-warnings 0 .
echo "3/4 component tests"
./node_modules/.bin/vitest run
if [ -z "${SKIP_BUILD:-}" ]; then
  echo "4/4 production build"
  ./node_modules/.bin/next build
else
  echo "4/4 build skipped"
fi
