#!/usr/bin/env bash
# Waisen auf Paperless-Media: Dateien unter originals/legacy/ ohne aktiven API-Eintrag.
#
# Paperless kann Dateien nicht „nachverlinken“ — nur löschen (wenn Inhalt woanders existiert)
# oder neu importieren (neues Doc). Dieses Script findet und bereinigt sichere Waisen.
#
# CT121:
#   ./scripts/legacy-media-orphans.sh audit
#   ./scripts/legacy-media-orphans.sh prune --dry-run
#   ./scripts/legacy-media-orphans.sh prune --apply
#
set -euo pipefail

ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"
API_BASE="${PAPERLESS_API:-http://127.0.0.1:8000}"
MEDIA_ROOT="${PAPERLESS_MEDIA_ROOT:-/mnt/paperless-media}"
LEGACY_SUB="${LEGACY_MEDIA_SUBDIR:-documents/originals/legacy}"
CMD="${1:-audit}"
shift || true
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
done

TOKEN=$(grep -m1 '^PAPERLESS_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
[[ -n "$TOKEN" ]] || { echo "FEHLER: kein PAPERLESS_TOKEN in $ENV_FILE" >&2; exit 1; }

export PAPERLESS_TOKEN="$TOKEN" API_BASE MEDIA_ROOT LEGACY_SUB APPLY CMD

python3 <<'PY'
import hashlib
import json
import os
import sys
import urllib.request
from collections import defaultdict

token = os.environ["PAPERLESS_TOKEN"]
api_base = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
media_root = os.environ.get("MEDIA_ROOT", "/mnt/paperless-media")
legacy_rel = os.environ.get("LEGACY_SUB", "documents/originals/legacy")
legacy_root = os.path.join(media_root, legacy_rel)
apply = os.environ.get("APPLY", "0") == "1"
cmd = os.environ.get("CMD", "audit")


def api_get(path):
    url = f"{api_base}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Token {token}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def md5_file(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def fetch_all_doc_ids():
    ids = []
    url = "/api/documents/?page_size=100&ordering=id"
    while url:
        data = api_get(url if url.startswith("/") else url.replace(api_base, ""))
        ids.extend(d["id"] for d in data.get("results", []))
        url = data.get("next")
    return ids


print("Lade API-Checksums (metadata) …", file=sys.stderr)
api_md5_to_ids = defaultdict(list)
api_md5_to_title = {}
doc_ids = fetch_all_doc_ids()
for i, doc_id in enumerate(doc_ids, 1):
    if i % 500 == 0:
        print(f"  … {i}/{len(doc_ids)}", file=sys.stderr)
    try:
        meta = api_get(f"/api/documents/{doc_id}/metadata/")
    except Exception as e:
        print(f"WARN: metadata #{doc_id}: {e}", file=sys.stderr)
        continue
    ch = (meta.get("original_checksum") or "").strip().lower()
    if not ch:
        continue
    api_md5_to_ids[ch].append(doc_id)
    if ch not in api_md5_to_title:
        doc = api_get(f"/api/documents/{doc_id}/")
        api_md5_to_title[ch] = (doc.get("title") or "")[:50]

print(f"API-Docs: {len(doc_ids)}, Checksums: {len(api_md5_to_ids)}", file=sys.stderr)

if not os.path.isdir(legacy_root):
    print(f"FEHLER: {legacy_root} nicht gefunden", file=sys.stderr)
    sys.exit(1)

disk_by_md5 = defaultdict(list)
disk_files = []
for dirpath, _dirs, files in os.walk(legacy_root):
    for fn in files:
        if not fn.lower().endswith(".pdf"):
            continue
        p = os.path.join(dirpath, fn)
        try:
            h = md5_file(p)
        except OSError as e:
            print(f"WARN: lesen: {p}: {e}", file=sys.stderr)
            continue
        rel = p.replace(legacy_root + os.sep, "").replace("\\", "/")
        disk_by_md5[h].append(p)
        disk_files.append((h, p, rel))

orphans_safe = []
orphans_dead = []

for h, paths in disk_by_md5.items():
    if h not in api_md5_to_ids:
        for p in paths:
            rel = p.replace(legacy_root + os.sep, "").replace("\\", "/")
            orphans_dead.append((h, p, rel))
        continue
    if len(paths) > 1:
        for p in sorted(paths)[1:]:
            rel = p.replace(legacy_root + os.sep, "").replace("\\", "/")
            orphans_safe.append((h, p, rel, api_md5_to_ids[h][0]))

print("=== Legacy-Media Waisen ===")
print(f"Root:     {legacy_root}")
print(f"PDFs auf Disk: {len(disk_files)}")
print(f"API-Checksums: {len(api_md5_to_ids)} (alle Docs, nicht nur legacy)")
print()
print(f"Sichere Waisen (MD5 in API, Extra-Datei):  {len(orphans_safe)}")
print(f"Tote Waisen (MD5 nicht in API):            {len(orphans_dead)}")
print()

if orphans_safe[:15]:
    print("Sichere Waisen — Inhalt existiert in API (Beispiele):")
    for h, p, rel, doc_id in orphans_safe[:15]:
        print(f"  #{doc_id}  {h[:12]}…  {rel}")
    if len(orphans_safe) > 15:
        print(f"  … +{len(orphans_safe) - 15} weitere")

if orphans_dead[:15]:
    print()
    print("Tote Waisen — kein API-Doc mit diesem MD5 (Beispiele):")
    for h, p, rel in orphans_dead[:15]:
        print(f"  {h[:12]}…  {rel}")
    if len(orphans_dead) > 15:
        print(f"  … +{len(orphans_dead) - 15} weitere")

print()
print("Hinweis: Paperless unterstützt kein nachträgliches „Verlinken“ von Dateien.")
print("  • Sichere Waisen → Datei löschen (Inhalt bleibt im aktiven Doc)")
print("  • Tote Waisen   → Datei löschen ODER neu importieren (legt neues Doc an)")

if cmd == "audit":
    print()
    print("Bereinigen:  legacy-media-orphans.sh prune --dry-run | --apply")
    sys.exit(0)

if cmd != "prune":
    print(f"Unbekannter Befehl: {cmd}", file=sys.stderr)
    sys.exit(1)

to_delete = [p for _h, p, _rel, _id in orphans_safe] + [p for _h, p, _rel in orphans_dead]
print()
if not to_delete:
    print("Nichts zu löschen.")
    sys.exit(0)

print(f"{'LÖSCHEN' if apply else 'DRY-RUN'}: {len(to_delete)} Dateien")
for p in to_delete[:20]:
    rel = p.replace(legacy_root + os.sep, "")
    print(f"  {'rm' if apply else '  '} {rel}")
if len(to_delete) > 20:
    print(f"  … +{len(to_delete) - 20} weitere")

if not apply:
    print()
    print("Mit --apply wirklich löschen.")
    sys.exit(0)

removed = 0
errors = 0
for p in to_delete:
    try:
        os.remove(p)
        removed += 1
    except OSError as e:
        print(f"WARN: {p}: {e}", file=sys.stderr)
        errors += 1

print(f"Fertig: {removed} gelöscht, {errors} Fehler.")
PY
