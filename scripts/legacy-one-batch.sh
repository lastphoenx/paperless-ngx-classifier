#!/usr/bin/env bash
# Ein NAS-Ordner → Paperless legacy (Vordergrund, kleine Chunks).
# Kein nohup, kein Docker-Anfassen, kein migrate-all.
#
# Voraussetzung in /opt/paperless/.env (einmalig):
#   PAPERLESS_CONSUMER_DELETE_DUPLICATES=true
#   PAPERLESS_OCR_MODE=skip
#   LEGACY_CONSUME_MARKERS=/legacy/
#
# Beispiel:
#   ./legacy-one-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Fano fano
#
set -euo pipefail

SRC="${1:?NAS-Quellpfad}"
SLUG="${2:?Batch-Name z.B. fano}"
CHUNK="${LEGACY_CHUNK_SIZE:-20}"
IMPORT="${LEGACY_IMPORT_SH:-/opt/paperless-scripts/legacy-import-batch.sh}"
CONSUME="${LEGACY_CONSUME_ROOT:-/mnt/paperless-data/consume/legacy}/$SLUG"
STATE="${LEGACY_MIGRATE_STATE_DIR:-/mnt/paperless-data/legacy-migrate}"
LIST="$STATE/imported/$SLUG.lst"

mkdir -p "$STATE/imported" "$(dirname "$CONSUME")"
touch "$LIST"
export LEGACY_IMPORTED_LIST="$LIST"

echo "=== legacy-one-batch: $SLUG ==="
echo "Quelle: $SRC | Chunk: $CHUNK | Consume: $CONSUME"
echo "Ctrl+C bricht ab — consume-Rest bleibt liegen, kein Hintergrund-Lauf."
echo ""

while true; do
  n_consume=$(find "$CONSUME" -type f -iname '*.pdf' 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$n_consume" -gt 0 ]]; then
    echo "[$(date '+%H:%M:%S')] warte: noch $n_consume PDFs in consume …"
    sleep 30
    continue
  fi

  export LEGACY_LAST_CHUNK_FILE="$STATE/last-chunk-$SLUG.txt"
  rm -f "$LEGACY_LAST_CHUNK_FILE"
  set +e
  "$IMPORT" "$SRC" "$SLUG" --chunk "$CHUNK"
  rc=$?
  set -e

  if [[ "$rc" -eq 2 ]]; then
    echo "[$(date '+%H:%M:%S')] fertig — keine ausstehenden PDFs mehr für $SLUG"
    break
  fi
  if [[ "$rc" -ne 0 ]]; then
    echo "FEHLER: import exit $rc" >&2
    exit 1
  fi

  if [[ -f "$LEGACY_LAST_CHUNK_FILE" ]]; then
    while IFS= read -r rel; do
      [[ -n "$rel" ]] && grep -qxF "$rel" "$LIST" 2>/dev/null || echo "$rel" >>"$LIST"
    done <"$LEGACY_LAST_CHUNK_FILE"
    sort -u -o "$LIST" "$LIST"
    rm -f "$LEGACY_LAST_CHUNK_FILE"
  fi

  echo "[$(date '+%H:%M:%S')] Chunk kopiert — Paperless verarbeitet …"
done

echo "=== $SLUG abgeschlossen ==="
