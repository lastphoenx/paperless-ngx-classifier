#!/usr/bin/env bash
# Gruppen von Referenz-User auf Ziel-User kopieren (Paperless REST API).
# Beispiel: ./scripts/paperless-copy-user-groups.sh user-a akadmin
set -euo pipefail

TARGET="${1:?Ziel-Username (z.B. akadmin)}"
SOURCE="${2:?Referenz-Username (z.B. user-a)}"

ENV_FILE="${ENV_FILE:-/opt/paperless/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

TOKEN="${PAPERLESS_TOKEN:-${PAPERLESS_API_TOKEN:-}}"
: "${TOKEN:?PAPERLESS_TOKEN fehlt}"

export PAPERLESS_TOKEN="$TOKEN"
export COPY_TARGET="$TARGET"
export COPY_SOURCE="$SOURCE"

python3 <<'PY'
import json
import os
import urllib.request

token = os.environ["PAPERLESS_TOKEN"].strip().strip('"')
base = os.environ.get("PAPERLESS_INTERNAL_URL", "http://127.0.0.1:8000").rstrip("/")
accept = os.environ.get("PAPERLESS_API_ACCEPT", "application/json; version=9")
headers = {"Authorization": f"Token {token}", "Accept": accept, "Content-Type": "application/json"}


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


users = {u["username"].lower(): u for u in api("GET", "/api/users/?page_size=100")["results"]}
src = users.get(os.environ["COPY_SOURCE"].lower())
tgt = users.get(os.environ["COPY_TARGET"].lower())
if not src:
    raise SystemExit(f"Referenz «{os.environ['COPY_SOURCE']}» nicht gefunden")
if not tgt:
    raise SystemExit(f"Ziel «{os.environ['COPY_TARGET']}» nicht gefunden")

new_groups = src.get("groups") or []
print(f"Kopiere Gruppen {new_groups} von {src['username']} → {tgt['username']} (id={tgt['id']})")
payload = {
    "username": tgt["username"],
    "email": tgt.get("email") or "",
    "groups": new_groups,
}
api("PATCH", f"/api/users/{tgt['id']}/", payload)
print("OK — User neu einloggen oder Session aktualisieren.")
PY
