#!/usr/bin/env bash
# Ship the application to the host that Terraform created, and start it.
#
#   terraform -chdir=infra/terraform apply      # once: creates the host
#   bash deploy/deploy.sh                       # every release
#
# The image is built locally (the scraped database is baked in) and sent over SSH with
# `docker save | docker load`, so no registry is involved and the data never leaves
# machines we control. Host address, login user and key all come from Terraform outputs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF="$ROOT/infra/terraform"
tf_out() { terraform -chdir="$TF" output -raw "$1" | tr -d '\r'; }

PUBLIC_IP="$(tf_out public_ip)"
SITE_HOST="$(tf_out site_host)"
SSH_USER="$(tf_out ssh_user)"
KEY="$TF/.secrets/estatequery.pem"
REMOTE="$SSH_USER@$PUBLIC_IP"
SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
TAG="estatequery:$(date +%Y%m%d-%H%M%S)"

cd "$ROOT"
[ -f data/estatequery.db ] || { echo "data/estatequery.db is missing; run: uv run estatequery build-db" >&2; exit 1; }
[ -f .env ] || { echo ".env is missing" >&2; exit 1; }
chmod 600 "$KEY" 2>/dev/null || true

# Release gate: the tests always run; the evaluation gate (about 3 minutes and about $1 of API
# calls, thresholds in eval/gate.json) runs unless --skip-gate is given. A skipped gate is said so.
SKIP_GATE=""
for arg in "$@"; do
  case "$arg" in
    --skip-gate) SKIP_GATE=1 ;;
    *) echo "unknown option: $arg (only --skip-gate is accepted)" >&2; exit 2 ;;
  esac
done

echo "== checking the database carries passage vectors (the image runs hybrid retrieval)"
uv run --no-sync python - <<'PY' || { echo "data/estatequery.db has no passage vectors; run: uv run estatequery build-db --embeddings" >&2; exit 1; }
import sqlite3, sys
conn = sqlite3.connect("file:data/estatequery.db?mode=ro", uri=True)
sys.exit(0 if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'chunk_vectors'").fetchone()[0] else 1)
PY

echo "== running the tests"
uv run --no-sync --group scrape pytest -q || { echo "tests failed; not deploying" >&2; exit 1; }
if [ -n "$SKIP_GATE" ]; then
  echo "== evaluation gate SKIPPED (--skip-gate): this release was not checked against eval/gate.json"
else
  echo "== running the evaluation gate"
  RETRIEVAL_MODE=hybrid uv run --no-sync estatequery eval --models anthropic:claude-sonnet-5-5,anthropic:claude-haiku-5-5 --gate \
    || { echo "evaluation gate failed; not deploying. Read eval/results.md, or override with --skip-gate" >&2; exit 1; }
fi

echo "== building $TAG"
docker build -q -t "$TAG" -t estatequery:latest . >/dev/null

echo "== waiting for the host to finish first-boot setup ($REMOTE)"
ready=""
for _ in $(seq 1 40); do
  if ssh "${SSH_OPTS[@]}" "$REMOTE" 'docker compose version >/dev/null 2>&1 && test -w /opt/estatequery' 2>/dev/null; then ready=1; break; fi
  sleep 10
done
[ -n "$ready" ] || { echo "host never became ready; check: ssh ${SSH_OPTS[*]} $REMOTE 'sudo tail -30 /var/log/cloud-init-output.log'" >&2; exit 1; }

echo "== shipping image"
docker save "$TAG" | gzip | ssh "${SSH_OPTS[@]}" "$REMOTE" 'gunzip | docker load'

echo "== shipping configuration"
# The server's .env is the local one with the public hostname and image tag set.
TMP_ENV="$(mktemp)"
trap 'rm -f "$TMP_ENV"' EXIT
grep -v -E '^(SITE_HOST|APP_IMAGE)=' .env > "$TMP_ENV"
printf 'SITE_HOST=%s\nAPP_IMAGE=%s\n' "$SITE_HOST" "$TAG" >> "$TMP_ENV"
ssh "${SSH_OPTS[@]}" "$REMOTE" 'mkdir -p /opt/estatequery/deploy'
scp "${SSH_OPTS[@]}" docker-compose.yml Caddyfile "$REMOTE:/opt/estatequery/"
scp "${SSH_OPTS[@]}" deploy/chat_history_schema.sql "$REMOTE:/opt/estatequery/deploy/"
scp "${SSH_OPTS[@]}" "$TMP_ENV" "$REMOTE:/opt/estatequery/.env"

echo "== starting"
ssh "${SSH_OPTS[@]}" "$REMOTE" 'cd /opt/estatequery && chmod 600 .env && docker compose up -d --no-build --remove-orphans && docker image prune -f >/dev/null'

echo "== checking https://$SITE_HOST/healthz"
for _ in $(seq 1 30); do
  if curl -fsS -m 10 "https://$SITE_HOST/healthz"; then
    echo
    # The site must be behind its login. If a request with no credentials is let in, the gate
    # is not active, so do not report success.
    gate="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "https://$SITE_HOST/")"
    if [ "$gate" != "401" ]; then
      echo "WARNING: https://$SITE_HOST/ answered $gate without credentials, expected 401. The site is NOT password protected." >&2
      exit 1
    fi
    echo "login required: yes (401 without credentials)"
    # The retrieval models load in the background after start-up; wait for the health check to say so.
    retrieval=""
    for _ in $(seq 1 15); do
      retrieval="$(curl -fsS -m 10 "https://$SITE_HOST/healthz" | sed -n 's/.*"retrieval":"\([^"]*\)".*/\1/p')"
      [ "$retrieval" = "hybrid" ] && break
      sleep 4
    done
    if [ "$retrieval" = "hybrid" ]; then
      echo "retrieval: hybrid (models loaded)"
    else
      echo "WARNING: retrieval is '$retrieval', not hybrid. Searches use BM25; check: ssh ${SSH_OPTS[*]} $REMOTE 'cd /opt/estatequery && docker compose logs --tail 50 app'" >&2
    fi
    echo "live: https://$SITE_HOST"
    exit 0
  fi
  sleep 6
done
echo "the site did not answer over HTTPS yet; check: ssh ${SSH_OPTS[*]} $REMOTE 'cd /opt/estatequery && docker compose logs --tail 50'" >&2
exit 1
