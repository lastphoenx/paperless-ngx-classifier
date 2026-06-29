#!/usr/bin/env bash
# Paperless: Mehrfach-importierte Legacy-Docs finden und Duplikate löschen.
# Standard: nur Tag "legacy", pro Dateiname das älteste Doc (#niedrigste ID) behalten.
#
# Auf CT121:
#   ./legacy-dedupe-imports.sh audit
#   ./legacy-dedupe-imports.sh analyze    # Dubletten: Hash, Speicherpfad, Disk-Pfad
#   ./legacy-dedupe-imports.sh delete --dry-run
#   ./legacy-dedupe-imports.sh delete --apply
#   ./legacy-dedupe-imports.sh audit --added-date 2026-06-28
#
set -euo pipefail

ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"
API_BASE="${PAPERLESS_API:-http://127.0.0.1:8000}"
LEGACY_TAG="${LEGACY_DEDUPE_TAG:-legacy}"
MIN_COPIES="${LEGACY_DEDUPE_MIN:-2}"
ADDED_DATE=""
APPLY=0
CMD="${1:-audit}"
shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    --tag) LEGACY_TAG="${2:?}"; shift 2 ;;
    --min) MIN_COPIES="${2:?}"; shift 2 ;;
    --added-date) ADDED_DATE="${2:?}"; shift 2 ;;
    --all-tags) LEGACY_TAG=""; shift ;;
    -h|--help)
      sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
      echo ""
      echo "Befehle: audit | analyze | delete"
      echo ""
      echo "Optionen: --added-date YYYY-MM-DD  nur Docs mit Hinzugefügt-am (Paperless added)"
      echo "          --min N  --tag NAME  --all-tags  --apply  --dry-run"
      exit 0
      ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
done

TOKEN=$(grep -m1 '^PAPERLESS_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
[[ -n "$TOKEN" ]] || { echo "FEHLER: kein PAPERLESS_TOKEN in $ENV_FILE" >&2; exit 1; }

export PAPERLESS_TOKEN="$TOKEN" API_BASE LEGACY_TAG MIN_COPIES ADDED_DATE APPLY CMD
export PAPERLESS_MEDIA_ROOT="${PAPERLESS_MEDIA_ROOT:-/mnt/paperless-media}"

python3 <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict

token = os.environ["PAPERLESS_TOKEN"]
api_base = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
legacy_tag = os.environ.get("LEGACY_TAG", "legacy")
min_copies = int(os.environ.get("MIN_COPIES", "2") or "2")
added_date = os.environ.get("ADDED_DATE", "").strip()
apply = os.environ.get("APPLY", "0") == "1"
cmd = os.environ.get("CMD", "audit")


def api_request(method, path, body=None):
    url = f"{api_base}{path}"
    data = None
    headers = {"Authorization": f"Token {token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))


def api_delete(path):
    url = f"{api_base}{path}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Token {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        print(f"WARN: DELETE {path} → {e.code} {err}", file=sys.stderr)
        return False


def fetch_all_docs():
    docs = []
    if legacy_tag:
        url = f"/api/documents/?tags__name__iexact={legacy_tag}&page_size=100&ordering=id"
    else:
        url = "/api/documents/?page_size=100&ordering=id"
    if added_date:
        url += f"&added__date={added_date}"
    while url:
        if url.startswith("http"):
            path = url.replace(api_base, "", 1)
        else:
            path = url
        data = api_request("GET", path)
        if isinstance(data, list):
            results = data
            url = None
        else:
            results = data.get("results", [])
            url = data.get("next")
        docs.extend(results)
        if len(docs) % 500 == 0 and len(docs) > 0:
            print(f"  … {len(docs)} Docs geladen", file=sys.stderr)
    return docs


def added_ymd(doc):
    added = (doc.get("added") or doc.get("created") or "").strip()
    return added[:10] if len(added) >= 10 else ""


def added_short(doc):
    added = (doc.get("added") or doc.get("created") or "").strip()
    return added[:19].replace("T", " ") if added else "?"


def group_key(doc):
    fn = (doc.get("original_file_name") or doc.get("title") or "").strip()
    return os.path.basename(fn).lower() if fn else f"__id_{doc.get('id')}"


media_root = os.environ.get("PAPERLESS_MEDIA_ROOT", "/mnt/paperless-media")
orig_root = os.path.join(media_root, "documents", "originals")
_sp_cache = {}
_md5_disk_cache = None


def storage_paths():
    global _sp_cache
    if _sp_cache:
        return _sp_cache
    url = "/api/storage_paths/?page_size=200"
    while url:
        data = api_request("GET", url if url.startswith("/") else url.replace(api_base, "", 1))
        for sp in data.get("results", []):
            _sp_cache[sp["id"]] = sp
        url = data.get("next")
    return _sp_cache


def doc_metadata(doc_id):
    return api_request("GET", f"/api/documents/{doc_id}/metadata/")


def disk_md5_index():
    global _md5_disk_cache
    if _md5_disk_cache is not None:
        return _md5_disk_cache
    import hashlib
    idx = defaultdict(list)
    if not os.path.isdir(orig_root):
        _md5_disk_cache = idx
        return idx
    for dirpath, _dirs, files in os.walk(orig_root):
        for fn in files:
            if not fn.lower().endswith(".pdf"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                h = hashlib.md5()
                with open(p, "rb") as f:
                    while b := f.read(1 << 20):
                        h.update(b)
                ch = h.hexdigest()
            except OSError:
                continue
            rel = p.replace(orig_root + os.sep, "").replace("\\", "/")
            idx[ch].append(rel)
    _md5_disk_cache = idx
    return idx


def sp_label(doc):
    sp_id = doc.get("storage_path")
    if not sp_id:
        return "—", "none/"
    sp = storage_paths().get(sp_id, {})
    name = sp.get("name") or "?"
    path = sp.get("path") or name
    return name, path


def disk_paths_for_doc(doc):
    doc_id = doc.get("id")
    try:
        meta = doc_metadata(doc_id)
        ch = (meta.get("original_checksum") or "").lower()
    except Exception:
        ch = ""
    paths = disk_md5_index().get(ch, []) if ch else []
    return ch, paths


print("Lade Dokumente …", file=sys.stderr)
docs = fetch_all_docs()
if added_date:
    docs = [d for d in docs if added_ymd(d) == added_date]
label = f" (Tag: {legacy_tag})" if legacy_tag else ""
when = f", Hinzugefügt: {added_date}" if added_date else ""
print(f"Geladen: {len(docs)} Docs{label}{when}", file=sys.stderr)

by_name = defaultdict(list)
for doc in docs:
    by_name[group_key(doc)].append(doc)

unique_names = len(by_name)
dup_groups = []
for name, group in sorted(by_name.items(), key=lambda x: (-len(x[1]), x[0])):
    if len(group) < min_copies:
        continue
    group.sort(key=lambda d: int(d.get("id") or 0))
    keep = group[0]
    delete = group[1:]
    dup_groups.append((name, keep, delete))

title = "=== Legacy Dedupe ==="
if added_date:
    title = f"=== Legacy Dedupe (hinzugefügt {added_date}) ==="

if not dup_groups:
    print(title)
    print(f"Docs gesamt:           {len(docs)}")
    print(f"Einzigartige Namen:    {unique_names}")
    print("Keine Duplikat-Gruppen gefunden.")
    sys.exit(0)

total_delete = sum(len(g[2]) for g in dup_groups)
print(title)
print(f"Docs gesamt:                      {len(docs)}")
print(f"Einzigartige Dateinamen:          {unique_names}")
print(f"Duplikat-Gruppen (≥{min_copies}×): {len(dup_groups)}")
print(f"Docs behalten:                    {len(dup_groups)}")
print(f"Docs löschen:                     {total_delete}")
print()

for name, keep, delete in dup_groups[:50]:
    all_ids = [keep] + delete
    ids_all = ", ".join(f"#{d['id']}" for d in all_ids)
    ids_del = ", ".join(f"#{d['id']}" for d in delete)
    print(f"  {name}: {len(all_ids)}× [{ids_all}]")
    print(f"    → behalten #{keep['id']} ({added_ymd(keep)}), löschen {ids_del}")
    for doc in all_ids:
        tag = "BEHALTEN" if doc is keep else "löschen"
        print(f"       #{doc['id']:>5}  hinzugefügt {added_short(doc)}  [{tag}]")
if len(dup_groups) > 50:
    print(f"  … +{len(dup_groups) - 50} weitere Gruppen")

if cmd == "audit":
    print()
    print("Detailanalyse:  legacy-dedupe-imports.sh analyze")
    print("Löschen:")
    print("  legacy-dedupe-imports.sh delete --dry-run")
    print("  legacy-dedupe-imports.sh delete --apply")
    sys.exit(0)

if cmd == "analyze":
    print("=== Dubletten-Analyse (Hash + Pfade) ===")
    print(f"Media: {orig_root}")
    print()
    same_hash_groups = 0
    diff_hash_groups = 0
    for name, keep, delete in dup_groups:
        all_docs = [keep] + delete
        checksums = {}
        for doc in all_docs:
            ch, paths = disk_paths_for_doc(doc)
            checksums[doc["id"]] = ch
        unique_ch = {c for c in checksums.values() if c}
        same = len(unique_ch) <= 1 and len(unique_ch) > 0
        if same:
            same_hash_groups += 1
        else:
            diff_hash_groups += 1
        flag = "GLEICHER Hash" if same else "VERSCHIEDENE Hashes"
        print(f"── {name}  ({flag})")
        for doc in all_docs:
            role = "BEHALTEN" if doc is keep else "LÖSCHEN"
            sp_name, sp_path = sp_label(doc)
            ch, paths = disk_paths_for_doc(doc)
            chs = ch[:16] + "…" if ch else "—"
            print(f"   #{doc['id']:<5}  {added_short(doc)}  [{role}]")
            print(f"           SP: {sp_name}  ({sp_path})")
            print(f"           MD5: {chs}")
            if paths:
                for p in paths[:3]:
                    print(f"           Disk: {p}")
                if len(paths) > 3:
                    print(f"           … +{len(paths) - 3} weitere Pfade")
            else:
                print("           Disk: (nicht gefunden unter originals/)")
        print()
    print(f"Zusammenfassung: {len(dup_groups)} Gruppen — {same_hash_groups} gleicher Hash, {diff_hash_groups} unterschiedliche Hashes")
    print("Bei unterschiedlichen Hashes: OCR/Prepare-Unterschied — trotzdem gleicher Dateiname.")
    sys.exit(0)

if cmd != "delete":
    print(f"Unbekannter Befehl: {cmd}", file=sys.stderr)
    sys.exit(1)

print()
if not apply:
    print("DRY-RUN — nichts gelöscht. Mit --apply wirklich löschen.")
    sys.exit(0)

deleted = 0
failed = 0
for _name, keep, delete in dup_groups:
    for doc in delete:
        doc_id = doc.get("id")
        if doc_id is None:
            continue
        if api_delete(f"/api/documents/{doc_id}/"):
            deleted += 1
            if deleted % 25 == 0:
                print(f"  … {deleted} gelöscht", file=sys.stderr)
        else:
            failed += 1

print(f"Fertig: {deleted} Duplikate gelöscht, {failed} Fehler.")
PY
