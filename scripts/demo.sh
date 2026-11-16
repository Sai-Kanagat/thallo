#!/usr/bin/env bash
# Try Thallo on this laptop. Nothing is published, sent or spent.
#
#   scripts/demo.sh                  start with the fictional bakery: throwaway Postgres, demo data, API, web app
#   scripts/demo.sh --brand sai      start with Sai Kanagat's own brand and the facts his site states
#   scripts/demo.sh stop             stop everything and delete the demo's data and keys
#
# It never reads .env and never touches Supabase or any DATABASE_URL you have set: the database is
# a Docker container of its own (thallo-demo-pg, port 54339). Keys are made fresh for this run in
# a private folder outside the repository and deleted by `stop`. The publisher is never started.
# See docs/DEMO.md.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

STATE="${DEMO_STATE:-$(cd "${TMPDIR:-/tmp}" && pwd -P)/thallo-demo}"
CONTAINER="${DEMO_CONTAINER:-thallo-demo-pg}"
PG_PORT="${DEMO_PG_PORT:-54339}"
API_PORT="${DEMO_API_PORT:-8765}"
WEB_PORT="${DEMO_WEB_PORT:-3300}"
WEB_URL="http://localhost:$WEB_PORT"
case "$(basename "$STATE")" in  # stop deletes this folder, so it must look like a demo folder
  thallo-demo*) ;;
  *) echo "DEMO_STATE must be a folder named thallo-demo or thallo-demo-something."; exit 2 ;;
esac

PY="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ] && git rev-parse --git-common-dir >/dev/null 2>&1; then
  PY="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)/.venv/bin/python"  # worktree: use the main venv
fi

stop() {
  for name in web api; do
    if [ -f "$STATE/$name.pid" ]; then
      kill "$(cat "$STATE/$name.pid")" 2>/dev/null || true
    fi
  done
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$STATE"
  echo "Demo stopped: processes, database container, demo keys and demo files are gone."
}

BRAND="bakery"
while [ $# -gt 0 ]; do
  case "$1" in
    stop) stop; exit 0 ;;
    --brand) [ $# -ge 2 ] || { echo "usage: scripts/demo.sh [--brand bakery|sai] | stop"; exit 2; }; BRAND="$2"; shift 2 ;;
    --brand=*) BRAND="${1#--brand=}"; shift ;;
    *) echo "usage: scripts/demo.sh [--brand bakery|sai] | stop"; exit 2 ;;
  esac
done
case "$BRAND" in
  bakery) WHAT="the fictional bakery Forno Finto" ;;
  sai) WHAT="Sai Kanagat, from the facts his site states" ;;
  *) echo "Unknown brand '$BRAND'. Use bakery or sai."; exit 2 ;;
esac

# ---------------------------------------------------------------- preflight
[ -x "$PY" ] || { echo "No Python venv found (.venv). Create it first, see README."; exit 1; }
"$PY" -c "import uvicorn" 2>/dev/null || {
  echo "uvicorn is missing. Install it once:  $PY -m pip install 'uvicorn>=0.30,<1'"; exit 1; }
command -v docker >/dev/null && docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop."; exit 1; }
command -v node >/dev/null && command -v npm >/dev/null || { echo "Node.js and npm are needed for the web app."; exit 1; }
if [ -d "$STATE" ]; then
  echo "A demo is already set up in $STATE. Run scripts/demo.sh stop first."; exit 1
fi
for port in "$PG_PORT" "$API_PORT" "$WEB_PORT"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is in use. Free it or set DEMO_PG_PORT, DEMO_API_PORT, DEMO_WEB_PORT."; exit 1
  fi
done

mkdir -m 700 "$STATE"
mkdir -m 700 "$STATE/brand"
trap 'echo "Start failed. Logs are in $STATE (api.log, web.log). Run scripts/demo.sh stop to clean up."' ERR

# ---------------------------------------------------------------- throwaway keys, this run only
( umask 077; "$PY" - "$STATE/keys.env" <<'EOF'
import base64, secrets, sys
keys = {
    "DEMO_PG_PASSWORD": secrets.token_hex(16),
    "DEMO_APP_PASSWORD": secrets.token_hex(16),
    "THALLO_VAULT_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
    "THALLO_GATE_SIGNING_KEY": secrets.token_bytes(32).hex(),
    "THALLO_PROXY_STAMP_KEY": secrets.token_bytes(32).hex(),
    "THALLO_DEVICE_KEY": secrets.token_bytes(32).hex(),
}
with open(sys.argv[1], "w") as f:
    f.writelines(f"{k}={v}\n" for k, v in keys.items())
EOF
)
set -a
# shellcheck disable=SC1091
. "$STATE/keys.env"
set +a

# ---------------------------------------------------------------- database
echo "1/5 throwaway Postgres ($CONTAINER on port $PG_PORT)"
docker run -d --rm --name "$CONTAINER" -e POSTGRES_PASSWORD="$DEMO_PG_PASSWORD" -p "127.0.0.1:$PG_PORT:5432" \
  postgres:17-alpine >/dev/null
for _ in $(seq 1 60); do
  docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 1
done
sleep 1
ADMIN_URL="postgresql://postgres:$DEMO_PG_PASSWORD@127.0.0.1:$PG_PORT/postgres"
APP_URL="postgresql://thallo_demo_app:$DEMO_APP_PASSWORD@127.0.0.1:$PG_PORT/postgres"

echo "2/5 migrations and the app role"
DEMO_ADMIN_URL="$ADMIN_URL" "$PY" - <<'EOF'
import os, sys
sys.path.insert(0, "scripts")
import psycopg
from psycopg import sql
from migrate import migrate
url = os.environ["DEMO_ADMIN_URL"]
print(f"   applied {len(migrate(url))} migrations")
with psycopg.connect(url, autocommit=True) as conn:  # like scripts/provision_db.py, for Docker only
    conn.execute(sql.SQL("CREATE ROLE thallo_demo_app LOGIN INHERIT NOSUPERUSER NOBYPASSRLS PASSWORD {} IN ROLE cortex_app")
                 .format(sql.Literal(os.environ["DEMO_APP_PASSWORD"])))
EOF

echo "3/5 demo data ($WHAT)"
DEMO_BRAND="$BRAND" DEMO_ADMIN_URL="$ADMIN_URL" DEMO_APP_URL="$APP_URL" THALLO_BRAND_ROOT="$STATE/brand" \
  PYTHONPATH="$REPO" \
  "$PY" scripts/demo_seed.py

# ---------------------------------------------------------------- web build
echo "4/5 web app build (first run downloads packages with npm ci)"
(
  cd web
  export NEXT_TELEMETRY_DISABLED=1 THALLO_API_ORIGIN="http://127.0.0.1:$API_PORT"
  if [ ! -d node_modules ]; then
    npm ci --no-audit --no-fund >"$STATE/npm.log" 2>&1 || {
      echo "npm ci failed (it needs the network the first time). See $STATE/npm.log"; exit 1; }
  fi
  ./node_modules/.bin/next build >"$STATE/build.log" 2>&1 || { echo "Web build failed. See $STATE/build.log"; exit 1; }
)

# ---------------------------------------------------------------- start
echo "5/5 starting the API and the web app"
COMMON=(
  "PATH=$PATH" "HOME=$HOME" "PYTHONPATH=$REPO"
  "DATABASE_URL=$APP_URL" "DATABASE_ADMIN_URL=$ADMIN_URL"
  "THALLO_ENVIRONMENT=development" "THALLO_DEV_CONSOLE_LOGIN=1"
  "THALLO_WEB_ORIGIN=$WEB_URL,http://127.0.0.1:$WEB_PORT" "THALLO_WEB_PROXY_HOSTS=127.0.0.1"
  "THALLO_BRAND_ROOT=$STATE/brand"
  "THALLO_VAULT_KEY=$THALLO_VAULT_KEY" "THALLO_GATE_SIGNING_KEY=$THALLO_GATE_SIGNING_KEY"
  "THALLO_PROXY_STAMP_KEY=$THALLO_PROXY_STAMP_KEY" "THALLO_DEVICE_KEY=$THALLO_DEVICE_KEY"
)
env -i "${COMMON[@]}" "$PY" -c "
import uvicorn
from api.app import create_app, services_from_env
uvicorn.run(create_app(services_from_env()), host='127.0.0.1', port=$API_PORT, log_level='warning')
" >"$STATE/api.log" 2>&1 &
echo $! >"$STATE/api.pid"
(
  cd web
  env -i "PATH=$PATH" "HOME=$HOME" NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT="$WEB_PORT" HOSTNAME=127.0.0.1 \
    THALLO_API_ORIGIN="http://127.0.0.1:$API_PORT" THALLO_PROXY_STAMP_KEY="$THALLO_PROXY_STAMP_KEY" \
    node server.mjs >"$STATE/web.log" 2>&1 &
  echo $! >"$STATE/web.pid"
)
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null "$WEB_URL/login" 2>/dev/null && curl -sS -o /dev/null "http://127.0.0.1:$API_PORT/api/session" \
    2>/dev/null && break
  sleep 1
done
curl -fsS -o /dev/null "$WEB_URL/login" || { echo "The web app did not start. See $STATE/web.log and $STATE/api.log"; exit 1; }
trap - ERR

cat <<EOF

Thallo demo is running: $WHAT. Nothing is published, sent or spent.

  Open      $WEB_URL      (Chrome, Edge or Firefox; Safari refuses the secure cookie on http)
  Email     demo@thallo.test
  Code      sign-in codes are printed here, not emailed. Run:
              grep 'dev login' "$STATE/api.log" | tail -1

  Stop      scripts/demo.sh stop
EOF
