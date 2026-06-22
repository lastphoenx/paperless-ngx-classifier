#!/usr/bin/env bash
# Duplikat-Dateiaufgaben: Tasks vs. einzigartige Dateien (Paperless tasks-API = JSON-Array).
set -euo pipefail

ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"
TOKEN=$(grep -m1 '^PAPERLESS_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)

if [[ -z "$TOKEN" ]]; then
  echo "FEHLER: kein PAPERLESS_TOKEN in $ENV_FILE" >&2
  exit 1
fi

export PAPERLESS_TOKEN="$TOKEN"

python3 <<'PY'
import json
import os
import re
import urllib.request

token = os.environ["PAPERLESS_TOKEN"]
url = "http://127.0.0.1:8000/api/tasks/?task_name=consume_file&status=FAILURE"

req = urllib.request.Request(
    url,
    headers={"Authorization": f"Token {token}"},
)
with urllib.request.urlopen(req, timeout=60) as resp:
    data = json.load(resp)

if isinstance(data, dict):
    tasks = data.get("results", [])
else:
    tasks = data

files: set[str] = set()
targets: dict[str, int] = {}
dup_msgs = 0
other_fail = 0

for t in tasks:
    r = t.get("result") or ""
    if "duplicate of" not in r.lower():
        other_fail += 1
        continue
    dup_msgs += 1
    fn = t.get("task_file_name")
    if fn:
        files.add(fn.strip())
    m = re.search(r"Not consuming ([^:]+):", r)
    if m:
        files.add(m.group(1).strip())
    m2 = re.search(r"#(\d+)\)", r)
    if m2:
        doc = m2.group(1)
        targets[doc] = targets.get(doc, 0) + 1

old = sum(1 for k in targets if int(k) < 1000)
new = sum(1 for k in targets if int(k) >= 1000)

print("=== Legacy Duplikat-Audit ===")
print(f"Fehlgeschlagene Tasks gesamt:     {len(tasks)}")
print(f"  davon duplicate:                 {dup_msgs}")
print(f"  davon andere Fehler:             {other_fail}")
print(f"Einzigartige Dateinamen:           {len(files)}")
print(f"Einzigartige Ziel-Doks (#NNN):     {len(targets)}")
print(f"  Ziel #<1000 (vor Migration):    {old}")
print(f"  Ziel #>=1000 (heute importiert): {new}")
if dup_msgs > len(files):
    print(f"→ UI-Aufblähung: ~{dup_msgs - len(files)} Retry-Tasks für gleiche Dateien")
print()
print("Top-Ziele (mehrfach referenziert):")
for doc, n in sorted(targets.items(), key=lambda x: -x[1])[:15]:
    print(f"  #{doc}: {n}×")
PY

echo ""
grep '^PAPERLESS_CONSUMER_DELETE_DUPLICATES=' "$ENV_FILE" 2>/dev/null || echo "(DELETE_DUPLICATES nicht gesetzt)"
echo ""
pgrep -af 'legacy-migrate-resume|legacy-migrate-all' 2>/dev/null || echo "Kein legacy-migrate-resume/all aktiv"
