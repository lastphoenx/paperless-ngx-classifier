#!/usr/bin/env bash
# Paperless-Benutzer-Inventar inkl. E-Mail, Gruppen-Namen, Staff/Superuser.
# Authentik-UUID steht nicht in der Paperless-API — siehe Hinweis in Abschnitt 8.
# Auf CT 121 ausführen; Ausgabe (ohne Tokens) hierher kopieren.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/paperless/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

TOKEN="${PAPERLESS_TOKEN:-${PAPERLESS_API_TOKEN:-}}"
if [[ -z "$TOKEN" ]] && [[ -f "$ENV_FILE" ]]; then
  TOKEN=$(grep -m1 '^PAPERLESS_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
  [[ -z "$TOKEN" ]] && TOKEN=$(grep -m1 '^PAPERLESS_API_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
fi
TOKEN="${TOKEN%\"}"
TOKEN="${TOKEN#\"}"
: "${TOKEN:?PAPERLESS_TOKEN oder PAPERLESS_API_TOKEN fehlt — ENV_FILE setzen}"

export PAPERLESS_TOKEN="$TOKEN"
export INVENTORY_ENV_FILE="$ENV_FILE"
export INVENTORY_SOURCE_USER="${1:-user-a}"
export INVENTORY_TARGET_USER="${2:-}"

exec python3 <<'PY'
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


def section(title: str) -> None:
    print()
    print(f"---------- {title} ----------")


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email or "—"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "***"
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"


def api_bases() -> list[str]:
    candidates = [
        os.environ.get("INVENTORY_API_URL", "").strip(),
        os.environ.get("PAPERLESS_INTERNAL_URL", "").strip(),
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ]
    seen: set[str] = set()
    bases: list[str] = []
    for c in candidates:
        if not c:
            continue
        c = c.rstrip("/")
        if c not in seen:
            seen.add(c)
            bases.append(c)
    return bases or ["http://127.0.0.1:8000"]


_API_BASE = ""


def api_get(path: str) -> dict:
    global _API_BASE
    token = os.environ["PAPERLESS_TOKEN"].strip().strip('"').strip("'")
    accept = os.environ.get("PAPERLESS_API_ACCEPT", "application/json; version=9")
    errors: list[str] = []
    bases = [_API_BASE] if _API_BASE else api_bases()
    for base in bases:
        url = base + path
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Token {token}", "Accept": accept},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                data = json.loads(body)
            _API_BASE = base
            return data
        except urllib.error.HTTPError as exc:
            errors.append(f"{url} → HTTP {exc.code}")
        except urllib.error.URLError as exc:
            errors.append(f"{url} → {exc.reason}")
    raise SystemExit("API fehlgeschlagen:\n  " + "\n  ".join(errors))


groups_by_id = {
    g["id"]: g.get("name", "?")
    for g in api_get("/api/groups/?page_size=100").get("results", [])
}
users = api_get("/api/users/?page_size=100").get("results", [])

print("========== Paperless Benutzer-Inventar ==========")
print(f"Datum: {datetime.now().astimezone().isoformat(timespec='seconds')}")
print(f"API:   {_API_BASE or api_bases()[0]}")
print()

section("1) Gruppen (id → name)")
for gid in sorted(groups_by_id):
    print(f"  id={gid}  name={groups_by_id[gid]}")

section("2) Benutzer (id, username, E-Mail maskiert, staff, superuser, Gruppen)")
for u in sorted(users, key=lambda x: (x.get("username") or "").lower()):
    gids = u.get("groups") or []
    gnames = [groups_by_id.get(g, str(g)) for g in gids]
    print(
        f"  id={u.get('id')}  user={u.get('username')}  "
        f"email={mask_email(u.get('email') or '')}  "
        f"staff={u.get('is_staff')}  super={u.get('is_superuser')}  "
        f"groups={','.join(gnames) or '—'}"
    )

source = os.environ.get("INVENTORY_SOURCE_USER", "user-a").strip().lower()
target = os.environ.get("INVENTORY_TARGET_USER", "").strip().lower()

section(f"3) Referenz-Benutzer «{source}»")
src = next((u for u in users if (u.get("username") or "").lower() == source), None)
if not src:
    print(f"  Nicht gefunden — verfügbare Usernames: {', '.join(u.get('username','?') for u in users)}")
else:
    gids = src.get("groups") or []
    print(f"  id={src.get('id')}  groups={','.join(groups_by_id.get(g, str(g)) for g in gids)}")
    print(f"  staff={src.get('is_staff')}  super={src.get('is_superuser')}")

if target:
    section(f"4) Ziel-Benutzer «{target}» (Authentik/SSO — oft ohne Gruppen)")
    tgt = next((u for u in users if (u.get("username") or "").lower() == target), None)
    if not tgt:
        print(f"  Nicht gefunden.")
    else:
        gids = tgt.get("groups") or []
        print(f"  id={tgt.get('id')}  groups={','.join(groups_by_id.get(g, str(g)) for g in gids) or 'KEINE'}")
        if src and src.get("groups"):
            missing = set(src.get("groups") or []) - set(gids)
            if missing:
                print(f"  Fehlende Gruppen vs. {source}: {','.join(groups_by_id.get(g, str(g)) for g in missing)}")
                print("  Fix (Paperless Admin UI): User bearbeiten → gleiche Gruppen wie Referenz setzen.")
                print("  Oder: ./scripts/paperless-copy-user-groups.sh ZIEL USER REFERENZ")

section("5) .env Gruppen-IDs (post_consume / paper.manager)")
env_file = os.environ.get("INVENTORY_ENV_FILE", "/opt/paperless/.env")
if Path(env_file).is_file():
    for line in Path(env_file).read_text(encoding="utf-8", errors="replace").splitlines():
        if re.search(r"PAPERLESS_(VIEW|CHANGE)_GROUP", line):
            print(f"  {line.split('=')[0]}=…")

section("6) Authentik-ID")
print("  Nicht in Paperless REST-API enthalten.")
print("  In Authentik: Directory → Users → User → UUID (Overview).")
print("  Paperless-OAuth legt User beim ersten Login an — Gruppen manuell oder via Authentik-Group-Mapping.")

print()
print("========== Ende ==========")
PY
