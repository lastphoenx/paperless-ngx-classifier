#!/usr/bin/env bash
# Audit: Legacy-Migration-Status + originals/none vs legacy/ auf Paperless-Media.
#
# CT121:
#   ./scripts/legacy-originals-audit.sh
#   LEGACY_MIGRATE_STATE_DIR=/mnt/paperless-data/migrate-gemeinsam ./scripts/legacy-originals-audit.sh
#
set -euo pipefail

ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"
API_BASE="${PAPERLESS_API:-http://127.0.0.1:8000}"
MEDIA_ROOT="${PAPERLESS_MEDIA_ROOT:-/mnt/paperless-media}"
LEGACY_TAG="${LEGACY_TAG:-legacy}"
LEGACY_SP_NAME="${LEGACY_STORAGE_PATH_NAME:-Legacy}"
STATE_DIR="${LEGACY_MIGRATE_STATE_DIR:-}"

TOKEN=$(grep -m1 '^PAPERLESS_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
[[ -n "$TOKEN" ]] || { echo "FEHLER: kein PAPERLESS_TOKEN in $ENV_FILE" >&2; exit 1; }

export PAPERLESS_TOKEN="$TOKEN" API_BASE MEDIA_ROOT LEGACY_TAG LEGACY_SP_NAME STATE_DIR

python3 <<'PY'
import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict

token = os.environ["PAPERLESS_TOKEN"]
api_base = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
media_root = os.environ.get("MEDIA_ROOT", "/mnt/paperless-media")
legacy_tag = os.environ.get("LEGACY_TAG", "legacy")
legacy_sp_name = os.environ.get("LEGACY_SP_NAME", "Legacy")
state_dir = os.environ.get("STATE_DIR", "")

orig_root = os.path.join(media_root, "documents", "originals")


def api_get(path):
    url = f"{api_base}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Token {token}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def fetch_paginated(path):
    url = path if path.startswith("/") else f"/{path}"
    items = []
    while url:
        if url.startswith("http"):
            p = url.replace(api_base, "", 1)
        else:
            p = url
        data = api_get(p)
        if isinstance(data, list):
            items.extend(data)
            break
        items.extend(data.get("results", []))
        url = data.get("next")
    return items


def count_pdfs(root):
    n = 0
    if not os.path.isdir(root):
        return -1
    for _dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")):
                n += 1
    return n


print("=== A) Gemeinsam / Migrate-Status ===")
if state_dir:
    missing = os.path.join(state_dir, "nas-missing-import.tsv")
    if os.path.isfile(missing):
        lines = [ln for ln in open(missing, encoding="utf-8") if ln.strip() and not ln.startswith("relpath")]
        print(f"State: {state_dir}")
        print(f"  nas-missing-import.tsv: {len(lines)} offen")
    else:
        print(f"State: {state_dir} — keine missing.tsv")
else:
    for label, path in [
        ("gemeinsam", "/mnt/paperless-data/migrate-gemeinsam/nas-missing-import.tsv"),
        ("monika", "/mnt/paperless-data/migrate-monika/nas-missing-import.tsv"),
        ("thomas", "/mnt/paperless-data/migrate-thomas/nas-missing-import.tsv"),
    ]:
        if os.path.isfile(path):
            n = sum(1 for ln in open(path, encoding="utf-8") if ln.strip() and not ln.startswith("relpath"))
            print(f"  {label}: {n} fehlend" if n else f"  {label}: fertig (0 fehlend)")

print()
print("=== B) Dateien auf Disk (Paperless-Media) ===")
print(f"Root: {orig_root}")
for sub in ("none", "legacy"):
    p = os.path.join(orig_root, sub)
    c = count_pdfs(p)
    print(f"  originals/{sub}/: {c if c >= 0 else '— nicht vorhanden'} Dateien")

print()
print("=== C) API: Docs nach Speicherpfad + Tag legacy ===")
tags = {t["id"]: t["name"] for t in fetch_paginated("/api/tags/?page_size=200")}
legacy_tag_id = next((i for i, n in tags.items() if n.lower() == legacy_tag.lower()), None)
sp_list = fetch_paginated("/api/storage_paths/?page_size=200")
sp_by_id = {s["id"]: s for s in sp_list}
legacy_sp_id = next((s["id"] for s in sp_list if (s.get("name") or "").lower() == legacy_sp_name.lower()), None)

pending_tags = {n for n in tags.values() if n.startswith("pending")}

docs = fetch_paginated("/api/documents/?page_size=100&ordering=id")
print(f"Docs gesamt: {len(docs)}")

cat = Counter()
legacy_wrong_sp = []
legacy_ok = []
pipeline_none = []
pipeline_none_pending = []
none_with_legacy_tag = []

for doc in docs:
    doc_id = doc.get("id")
    sp_id = doc.get("storage_path")
    sp_name = sp_by_id.get(sp_id, {}).get("name", "") if sp_id else ""
    sp_path = sp_by_id.get(sp_id, {}).get("path", "") if sp_id else ""
    doc_tags = [tags.get(t, str(t)) for t in (doc.get("tags") or [])]
    has_legacy = legacy_tag_id and legacy_tag_id in (doc.get("tags") or [])
    has_pending = bool(set(doc_tags) & pending_tags)

    if sp_id is None or sp_name.lower() == "none" or sp_path == "":
        bucket = "api: kein Speicherpfad (→ none/)"
    elif sp_name.lower() == legacy_sp_name.lower() or (sp_path or "").startswith("legacy/"):
        bucket = "api: Legacy-Speicherpfad"
    else:
        bucket = f"api: {sp_name or sp_path or '?'}"
    cat[bucket] += 1

    title = (doc.get("title") or "")[:60]
    fn = doc.get("original_file_name") or ""

    if has_legacy and bucket != "api: Legacy-Speicherpfad":
        legacy_wrong_sp.append((doc_id, title, fn, doc_tags))
    if has_legacy and bucket == "api: Legacy-Speicherpfad":
        legacy_ok.append(doc_id)
    if bucket == "api: kein Speicherpfad (→ none/)":
        if has_legacy:
            none_with_legacy_tag.append((doc_id, title, fn))
        elif has_pending:
            pipeline_none_pending.append((doc_id, title, fn, doc_tags))
        else:
            pipeline_none.append((doc_id, title, fn, doc_tags))

print()
for k, v in sorted(cat.items(), key=lambda x: -x[1]):
    print(f"  {v:5d}  {k}")

print()
print(f"Legacy-Tag + korrekter SP:     {len(legacy_ok)}")
print(f"Legacy-Tag aber NICHT legacy/: {len(legacy_wrong_sp)}  ← nachziehen")
print(f"In none/ (API): Pipeline:     {len(pipeline_none)}  ← paper.manager / Routing")
print(f"In none/ (API): pending_*:    {len(pipeline_none_pending)}  ← Document Review")
print(f"In none/ (API): legacy-Tag:   {len(none_with_legacy_tag)}  ← Finalize fehlgeschlagen?")

if legacy_wrong_sp[:15]:
    print()
    print("Legacy-Tag, falscher Speicherpfad (max 15):")
    for row in legacy_wrong_sp[:15]:
        print(f"  #{row[0]}  {row[1]!r}  tags={row[3]}")

if none_with_legacy_tag[:15]:
    print()
    print("legacy-Tag aber kein SP (liegen vermutlich unter originals/none/):")
    for doc_id, title, fn in none_with_legacy_tag[:15]:
        print(f"  #{doc_id}  {title!r}  ({fn})")

if pipeline_none_pending[:10]:
    print()
    print("none/ + pending (paper.manager Document Review) — Beispiele:")
    for doc_id, title, fn, tgs in pipeline_none_pending[:10]:
        print(f"  #{doc_id}  {title!r}  tags={[t for t in tgs if t.startswith('pending')]}")

print()
print("=== D) Dubletten-Hinweis (Dateiname, legacy vs kein legacy) ===")
by_fn = defaultdict(list)
for doc in docs:
    fn = (doc.get("original_file_name") or doc.get("title") or "").strip()
    if not fn:
        continue
    key = os.path.basename(fn).lower()
    has_legacy = legacy_tag_id and legacy_tag_id in (doc.get("tags") or [])
    by_fn[key].append((doc["id"], has_legacy))

mixed = [(k, v) for k, v in by_fn.items() if len(v) > 1 and any(x[1] for x in v) and any(not x[1] for x in v)]
print(f"Gleicher Dateiname: legacy + Nicht-legacy: {len(mixed)}")
for k, v in sorted(mixed, key=lambda x: -len(x[1]))[:12]:
    ids = ", ".join(f"#{i}{'L' if lg else 'P'}" for i, lg in v)
    print(f"  {k}: {ids}  (L=legacy, P=Pipeline)")

print()
print("Fertig. none/ = Paperless-Default ohne Speicherpfad; legacy/ = Template legacy/{{title}}.")
print("Pipeline-Docs in none/ gehören zu paper.manager (pending_review etc.), nicht zu Legacy.")
PY
