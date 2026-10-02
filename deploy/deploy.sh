#!/usr/bin/env bash
# Ship the code (and, with --secrets, your dk.json + Google key) to the server and (re)start it.
#   deploy/deploy.sh root@SERVER media.example.com [--secrets] [--shared NETWORK] [--account-name NAME]
# --account-name NAME: create an account called NAME for every platform dk.json has an id for.
# --shared NETWORK: the server already has a web server on 80/443 in that Docker network. This stack
# then publishes no ports; see docs/setup/deploy.md for the one block that web server needs.
# Needs: ssh access to the server, rsync, python3 and docker on the server (bootstrap.sh).
set -euo pipefail
HOST="${1:?usage: deploy.sh user@server media-domain [--secrets]}"
DOMAIN="${2:?usage: deploy.sh user@server media-domain [--secrets]}"
SECRETS=""; SHARED=""; ACCOUNT_NAME=""
shift 2
while [ $# -gt 0 ]; do
  case "$1" in
    --secrets) SECRETS="--secrets" ;;
    --shared) SHARED="${2:?--shared needs the Docker network name of the existing web server}"; shift ;;
    --account-name) ACCOUNT_NAME="${2:?--account-name needs a name}"; shift ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
  shift
done
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
# printf %q keeps arguments with spaces intact when ssh joins them into one command line
ssh "$HOST" "bash -s $(printf '%q ' "$DOMAIN" "$SHARED" "$ACCOUNT_NAME")" <<'REMOTE_SCRIPT'
set -euo pipefail
cd /srv/dk/app/deploy
[ -s /srv/dk/secrets/dk.json ] || { echo "no /srv/dk/secrets/dk.json yet: re-run with --secrets"; exit 1; }
if [ ! -f .env ]; then
  umask 077
  printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" > .env
fi
sed -i '/^MEDIA_SITE=\|^COMPOSE_PROFILES=\|^DK_PROXY_NETWORK=/d' .env
printf 'MEDIA_SITE=%s\n' "$1" >> .env
if [ -n "$2" ]; then printf 'COMPOSE_PROFILES=shared\nDK_PROXY_NETWORK=%s\n' "$2" >> .env
else printf 'COMPOSE_PROFILES=standalone\n' >> .env; fi
docker compose -f compose.prod.yaml --env-file .env up -d --build
if [ -n "$3" ]; then
  docker compose -f compose.prod.yaml --env-file .env exec -T webserver dk account sync --name "$3"
fi
docker compose -f compose.prod.yaml --env-file .env ps
echo "==> checking the configuration inside the container (posts nothing)"
docker compose -f compose.prod.yaml --env-file .env run --rm --no-deps -T webserver dk check-setup || true
REMOTE_SCRIPT
echo "Done. Look at Dagster with:  ssh -L 3000:localhost:3000 $HOST   then open http://localhost:3000"
