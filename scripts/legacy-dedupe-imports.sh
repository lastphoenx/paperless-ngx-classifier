#!/usr/bin/env bash
# Paperless: Mehrfach-importierte Legacy-Docs finden und Duplikate löschen.
# Standard: nur Tag "legacy", pro Dateiname das älteste Doc (#niedrigste ID) behalten.
#
# Auf CT121:
#   ./legacy-dedupe-imports.sh audit
#   ./legacy-dedupe-imports.sh delete --dry-run
#   ./legacy-dedupe-imports.sh delete --apply
#
set -euo pipefail

ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"
API_BASE="${PAPERLESS_API:-http://127.0.0.1:8000}"
LEGACY_TAG="${LEGACY_DEDUPE_TAG:-legacy}"
MIN_COPIES="${LEGACY_DEDUPE_MIN:-2}"
APPLY=0
CMD="${1:-audit}"
shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    --tag) LEGACY_TAG="${2:?}"; shift 2 ;;
    --min) MIN_COPIES="${2:?}"; shift 2 ;;
    --all-tags) LEGACY_TAG=""; shift ;;
    -h|--help)
      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
done

TOKEN=$(grep -m1 '^PAPERLESS_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
[[ -n "$TOKEN" ]] || { echo "FEHLER: kein PAPERLESS_TOKEN in $ENV_FILE" >&2; exit 1; }

export PAPERLESS_TOKEN="$TOKEN" API_BASE LEGACY_TAG MIN_COPIES APPLY CMD

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


def group_key(doc):
    fn = (doc.get("original_file_name") or doc.get("title") or "").strip()
    return os.path.basename(fn).lower() if fn else f"__id_{doc.get('id')}"


print("Lade Dokumente …", file=sys.stderr)
docs = fetch_all_docs()
print(f"Geladen: {len(docs)} Docs" + (f" (Tag: {legacy_tag})" if legacy_tag else ""), file=sys.stderr)

by_name = defaultdict(list)
for doc in docs:
    by_name[group_key(doc)].append(doc)

dup_groups = []
for name, group in sorted(by_name.items(), key=lambda x: (-len(x[1]), x[0])):
    if len(group) < min_copies:
        continue
    group.sort(key=lambda d: int(d.get("id") or 0))
    keep = group[0]
    delete = group[1:]
    dup_groups.append((name, keep, delete))

if not dup_groups:
    print("Keine Duplikat-Gruppen gefunden.")
    sys.exit(0)

total_delete = sum(len(g[2]) for g in dup_groups)
print("=== Legacy Dedupe ===")
print(f"Duplikat-Gruppen (≥{min_copies}×): {len(dup_groups)}")
print(f"Docs behalten:                    {len(dup_groups)}")
print(f"Docs löschen:                     {total_delete}")
print()

for name, keep, delete in dup_groups[:50]:
    ids_del = ", ".join(f"#{d['id']}" for d in delete)
    print(f"  {name}: {1 + len(delete)}× → behalten #{keep['id']}, löschen {ids_del}")
if len(dup_groups) > 50:
    print(f"  … +{len(dup_groups) - 50} weitere Gruppen")

if cmd == "audit":
    print()
    print("Nur Auflistung. Löschen:")
    print("  legacy-dedupe-imports.sh delete --dry-run")
    print("  legacy-dedupe-imports.sh delete --apply")
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
