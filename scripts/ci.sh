#!/usr/bin/env bash
# The whole CI gate, runnable locally and in GitHub Actions. Needs no keys and no network
# beyond pulling the Postgres image once.
#
#   scripts/ci.sh            lint, rules, schema check, start a throwaway Postgres, all tests
#   SKIP_DB=1 scripts/ci.sh  everything except the database tests
#
# If DATABASE_URL is already set (as in GitHub Actions), that server is used instead of Docker.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY=python

echo "1/5 lint"
"$PY" -m ruff check cortex_operator/contracts scripts tests/contracts

echo "2/5 repository rules"
"$PY" scripts/check_rules.py

echo "3/5 migration matches the models"
"$PY" -m cortex_operator.contracts.ddl | diff -u db/migrations/0001_core.sql - \
  || { echo "regenerate: python -m cortex_operator.contracts.ddl > db/migrations/0001_core.sql"; exit 1; }

CONTAINER=""
cleanup() { [ -n "$CONTAINER" ] && docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

if [ -z "${SKIP_DB:-}" ] && [ -z "${DATABASE_URL:-}" ]; then
  echo "4/5 throwaway Postgres"
  CONTAINER="cortex-ci-$$"
  PORT="${CI_PG_PORT:-54330}"
  docker run -d --rm --name "$CONTAINER" -e POSTGRES_PASSWORD=ci -p "$PORT:5432" postgres:17-alpine >/dev/null
  for _ in $(seq 1 60); do
    docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1 && break
    sleep 1
  done
  sleep 1
  export DATABASE_URL="postgresql://postgres:ci@localhost:$PORT/postgres"
else
  echo "4/5 Postgres: ${DATABASE_URL:+using DATABASE_URL}${SKIP_DB:+skipped}"
fi

echo "5/5 tests"
"$PY" -m pytest -q
