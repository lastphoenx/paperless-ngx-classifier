#!/usr/bin/env bash
# Kurzüberblick: Paperless Dateiaufgaben + consume/legacy (was das UI zeigt).
set -euo pipefail

ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"
CONSUME="${LEGACY_CONSUME_ROOT:-/mnt/paperless-data/consume/legacy}"
TOKEN=$(grep -m1 '^PAPERLESS_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)

echo "=== Legacy Migration — Live-Status ==="
echo ""

if [[ -n "$TOKEN" ]]; then
  for spec in \
    "FAILURE:Fehlgeschlagen" \
    "SUCCESS:Abgeschlossen" \
    "STARTED:Gestartet" \
    "PENDING:Warteschlange"; do
    key="${spec%%:*}"
    name="${spec#*:}"
    n=$(curl -sf --connect-timeout 5 --max-time 15 \
      -H "Authorization: Token $TOKEN" \
      "http://127.0.0.1:8000/api/tasks/?task_name=consume_file&status=$key" \
      | python3 -c "
import sys, json
d = json.load(sys.stdin)
if isinstance(d, list):
    print(len(d))
elif isinstance(d, dict):
    print(d.get('count', len(d.get('results', []))))
else:
    print('?')
" 2>/dev/null || echo "?")
    echo "  Dateiaufgaben $name: $n"
  done

  legacy=$(curl -sf --connect-timeout 5 --max-time 10 \
    -H "Authorization: Token $TOKEN" \
    'http://127.0.0.1:8000/api/documents/?tags__name__iexact=legacy&page_size=1' \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('count','?'))" 2>/dev/null || echo "?")
  echo "  Dokumente Tag legacy: $legacy"
else
  echo "  (kein PAPERLESS_TOKEN — API-Abruf übersprungen)"
fi

echo ""
n_all=$(find "$CONSUME" -type f -iname '*.pdf' 2>/dev/null | wc -l | tr -d ' ')
echo "  PDFs in consume/legacy gesamt: $n_all"
for d in "$CONSUME"/*/; do
  [[ -d "$d" ]] || continue
  c=$(find "$d" -type f -iname '*.pdf' 2>/dev/null | wc -l | tr -d ' ')
  [[ "$c" -gt 0 ]] && echo "    $(basename "$d"): $c"
done

echo ""
pgrep -af 'legacy-migrate|legacy-import|legacy-one-batch' 2>/dev/null || echo "  Keine legacy-Skript-Prozesse aktiv"
echo ""
