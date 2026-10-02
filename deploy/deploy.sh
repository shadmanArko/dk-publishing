#!/usr/bin/env bash
# Ship the code (and, with --secrets, your dk.json + Google key) to the server and (re)start it.
#   deploy/deploy.sh root@SERVER media.example.com [--secrets]
# Needs: ssh access to the server, rsync, python3 and docker on the server (bootstrap.sh).
set -euo pipefail
HOST="${1:?usage: deploy.sh user@server media-domain [--secrets]}"
DOMAIN="${2:?usage: deploy.sh user@server media-domain [--secrets]}"
SECRETS="${3:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${DK_CONFIG_FILE:-$HOME/.config/dk-publishing/dk.json}"
REMOTE=/srv/dk

echo "==> code"
rsync -az --delete \
  --exclude .git --exclude .venv --exclude .env --exclude .dagster_home --exclude .media \
  --exclude __pycache__ --exclude '.*_cache' --exclude 'deploy/.env' \
  "$ROOT/" "$HOST:$REMOTE/app/"

if [ "$SECRETS" = "--secrets" ]; then
  echo "==> secrets (dk.json for the server, Google key)"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  python3 - "$CONFIG" "$DOMAIN" "$TMP/dk.json" <<'PY'
import json, sys
src, domain, out = sys.argv[1:]
data = json.load(open(src))
data["media"] = {**data.get("media", {}), "public_base_url": f"https://{domain}",
                 "public_dir": "/srv/public", "work_dir": "/srv/media"}
data["google"]["service_account_file"] = "google-key.json"   # the key sits next to dk.json
json.dump(data, open(out, "w"), indent=2)
PY
  cp "$(dirname "$CONFIG")/google-key.json" "$TMP/google-key.json"
  scp -q "$TMP/dk.json" "$TMP/google-key.json" "$HOST:$REMOTE/secrets/"
  ssh "$HOST" "chown 10001:10001 $REMOTE/secrets/* && chmod 600 $REMOTE/secrets/*"
fi

echo "==> start"
ssh "$HOST" bash -s "$DOMAIN" <<'REMOTE_SCRIPT'
set -euo pipefail
cd /srv/dk/app/deploy
[ -s /srv/dk/secrets/dk.json ] || { echo "no /srv/dk/secrets/dk.json yet: re-run with --secrets"; exit 1; }
if [ ! -f .env ]; then
  umask 077
  printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" > .env
fi
sed -i '/^MEDIA_SITE=/d' .env && printf 'MEDIA_SITE=%s\n' "$1" >> .env
docker compose -f compose.prod.yaml --env-file .env up -d --build
docker compose -f compose.prod.yaml --env-file .env ps
echo "==> checking the configuration inside the container (posts nothing)"
docker compose -f compose.prod.yaml --env-file .env run --rm --no-deps -T webserver dk check-setup || true
REMOTE_SCRIPT
echo "Done. Look at Dagster with:  ssh -L 3000:localhost:3000 $HOST   then open http://localhost:3000"
