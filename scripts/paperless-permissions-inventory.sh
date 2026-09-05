#!/usr/bin/env bash
# Phase-0-Inventar: Paperless-Benutzer, Gruppen, Speicherpfade, Beispiel-Dokumentrechte.
# Auf CT 121 (oder Host mit Paperless-API) ausführen, Ausgabe hierher kopieren.
# Kein jq nötig — nur python3 (stdlib).
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/paperless/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${PAPERLESS_TOKEN:?PAPERLESS_TOKEN fehlt — ENV_FILE setzen oder exportieren}"

export INVENTORY_ENV_FILE="$ENV_FILE"
export INVENTORY_NAS_ORIGINALS="${NAS_ORIGINALS:-}"
export INVENTORY_MANIFEST="${MANIFEST_PATH:-}"

exec python3 <<'PY'
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


def section(title: str) -> None:
    print()
    print(f"---------- {title} ----------")


def api_bases() -> list[str]:
    """Lokal auf dem Host: interne API, nicht die öffentliche Domain (liefert oft HTML)."""
    candidates = [
        os.environ.get("INVENTORY_API_URL", "").strip(),
        os.environ.get("PAPERLESS_INTERNAL_URL", "").strip(),
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        os.environ.get("PAPERLESS_URL", "").strip(),
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
    token = os.environ["PAPERLESS_TOKEN"]
    accept = os.environ.get("PAPERLESS_API_ACCEPT", "application/json; version=9")
    errors: list[str] = []
    bases = [_API_BASE] if _API_BASE else api_bases()
    for base in bases:
        url = base + path
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": accept},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "json" not in ctype.lower() and body[:1] not in (b"{", b"["):
                    snippet = body[:300].decode("utf-8", errors="replace")
                    errors.append(
                        f"{url} → HTTP {resp.status}, Content-Type={ctype!r}, "
                        f"kein JSON (Antwort beginnt mit: {snippet[:120]!r}…)"
                    )
                    continue
                data = json.loads(body)
            _API_BASE = base
            return data
        except urllib.error.HTTPError as exc:
            err_body = exc.read(300).decode("utf-8", errors="replace")
            errors.append(f"{url} → HTTP {exc.code}: {err_body[:200]}")
        except urllib.error.URLError as exc:
            errors.append(f"{url} → Verbindung fehlgeschlagen: {exc.reason}")
        except json.JSONDecodeError as exc:
            snippet = body[:300].decode("utf-8", errors="replace") if body else ""
            errors.append(f"{url} → JSON-Fehler {exc}: {snippet[:120]!r}…")
    hint = (
        "Tipp: Auf dem Paperless-Host PAPERLESS_INTERNAL_URL=http://127.0.0.1:8000 "
        "in /opt/paperless/.env setzen (PAPERLESS_URL bleibt die Domain für Browser)."
    )
    raise SystemExit("API-Aufruf fehlgeschlagen:\n  " + "\n  ".join(errors) + f"\n\n{hint}")


def perm_groups(permissions: dict | None, key: str) -> str:
    if not permissions:
        return ""
    val = permissions.get(key)
    if isinstance(val, list):
        return ",".join(str(x) for x in val)
    if isinstance(val, dict):
        users = val.get("users") or []
        groups = val.get("groups") or []
        return f"users={','.join(str(x) for x in users)}/groups={','.join(str(x) for x in groups)}"
    return str(val)


bases = api_bases()
print("========== Phase 0 — Paperless Permissions Inventar ==========")
print(f"Datum: {datetime.now().astimezone().isoformat(timespec='seconds')}")
print(f"API:   {bases[0]}  (Fallbacks: {', '.join(bases[1:]) or '—'})")
print()

section("1) Gruppen (id, name)")
for g in sorted(api_get("/api/groups/?page_size=100").get("results", []), key=lambda x: x.get("name", "")):
    print(f"  id={g.get('id')}  name={g.get('name')}")

section("2) Benutzer (id, username, superuser, group_ids)")
for u in api_get("/api/users/?page_size=100").get("results", []):
    groups = ",".join(str(x) for x in (u.get("groups") or []))
    print(
        f"  id={u.get('id')}  user={u.get('username')}  "
        f"super={u.get('is_superuser')}  groups={groups}"
    )

section("3) Aktuelle .env Gruppen-Konfiguration (paper.manager / post_consume)")
env_file = os.environ.get("INVENTORY_ENV_FILE", "/opt/paperless/.env")
if Path(env_file).is_file():
    for line in Path(env_file).read_text(encoding="utf-8", errors="replace").splitlines():
        if "PAPERLESS_VIEW" in line or "PAPERLESS_CHANGE" in line:
            if "GROUP" in line:
                print(f"  {line}")
else:
    print(f"  (kein {env_file})")

section("4) Speicherpfade in Paperless (id, name, path, permissions)")
for sp in api_get("/api/storage_paths/?page_size=200").get("results", []):
    perms = sp.get("permissions") or {}
    print(
        f"  id={sp.get('id')}  path={sp.get('path')}  name={sp.get('name')}  "
        f"owner={sp.get('owner')}  view={perm_groups(perms, 'view')}  "
        f"change={perm_groups(perms, 'change')}"
    )

section("5) Beispiel-Dokumente — Rechte nach Speicherpfad-Präfix")
for d in api_get("/api/documents/?page_size=20&ordering=-created").get("results", []):
    perms = d.get("permissions") or {}
    sp = d.get("storage_path")
    print(
        f"  doc={d.get('id')}  storage_path_id={sp if sp is not None else '?'}  "
        f"owner={d.get('owner')}  view_users={perm_groups(perms, 'view_users')}  "
        f"view_groups={perm_groups(perms, 'view_groups')}  "
        f"change_users={perm_groups(perms, 'change_users')}  "
        f"change_groups={perm_groups(perms, 'change_groups')}"
    )

section("6) NAS-Hauptordner unter originals/")
nas_candidates = [
    os.environ.get("INVENTORY_NAS_ORIGINALS", "").strip(),
    "/mnt/paperless-data/data/media/documents/originals",
    "/mnt/nas-legacy/Paperless/media/documents/originals",
    "/srv/nas/Paperless/media/documents/originals",
]
found_nas = False
for nas_root in nas_candidates:
    if not nas_root:
        continue
    p = Path(nas_root)
    if p.is_dir():
        found_nas = True
        print(f"  ({nas_root})")
        for name in sorted(p.iterdir()):
            if name.is_dir():
                print(f"  {name.name}")
if not found_nas:
    print("  (kein originals-Verzeichnis gefunden — NAS_ORIGINALS setzen)")

section("7) Manifest-Hauptordner (paper.manager)")
manifest_candidates = [
    os.environ.get("INVENTORY_MANIFEST", "").strip(),
    "/opt/paperless-scripts/manifest.json",
    "/opt/paperless-scripts/training/manifest.json",
]
found_manifest = False
for manifest in manifest_candidates:
    if not manifest:
        continue
    p = Path(manifest)
    if p.is_file():
        found_manifest = True
        data = json.loads(p.read_text(encoding="utf-8"))
        roots = sorted(
            {e.get("pfad", "").split("/")[0] for e in data.get("ordner", []) if e.get("pfad")}
        )
        print(f"  ({manifest})")
        for root in roots:
            print(f"  {root}")
if not found_manifest:
    print("  (kein manifest.json gefunden — MANIFEST_PATH setzen)")

print()
print("========== Ende — bitte komplette Ausgabe zurückschicken ==========")
PY
