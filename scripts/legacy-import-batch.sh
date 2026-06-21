#!/usr/bin/env bash
# Legacy-Altbestand: NAS-Ordner → consume/legacy/<batch>/ (Tags setzt post_consume per API)
#
# Aufruf (auf dem Paperless-Host CT 121, nach NFS-Mount):
#   ./scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen eltern-finanzen --limit 10
#   ./scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen lohn lohn --chunk 30
#
set -euo pipefail

LEGACY_TAG="${LEGACY_TAG:-legacy}"
CONSUME_LEGACY_ROOT="${LEGACY_CONSUME_ROOT:-/mnt/paperless-data/consume/legacy}"
DRY_RUN=0
LIMIT=0
CHUNK=0

usage() {
  cat <<'EOF'
Usage: legacy-import-batch.sh <NAS-QUELLPFAD> <BATCH-NAME> [Optionen]

Optionen:
  --limit N       Nur die ersten N PDFs (Test)
  --chunk N       Nächste N noch nicht importierte PDFs (--ignore-existing + imported.lst)
  --dry-run       Zeigt rsync an, schreibt nichts
  -h, --help      Diese Hilfe

Umgebungsvariablen:
  LEGACY_CONSUME_ROOT
  LEGACY_IMPORTED_LIST   Datei mit bereits verarbeiteten relative Pfaden (pro Batch)
EOF
}

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'
}

already_imported() {
  local rel="$1"
  [[ -z "${LEGACY_IMPORTED_LIST:-}" || ! -f "$LEGACY_IMPORTED_LIST" ]] && return 1
  grep -qxF "$rel" "$LEGACY_IMPORTED_LIST" 2>/dev/null
}

if [[ $# -lt 2 ]]; then
  usage
  exit 1
fi

SRC="$(cd "$1" && pwd)"
BATCH_RAW="$2"
shift 2

if [[ ! -d "$SRC" ]]; then
  echo "FEHLER: Quellpfad nicht gefunden: $1" >&2
  exit 1
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) LIMIT="${2:?}"; shift 2 ;;
    --chunk) CHUNK="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; usage; exit 1 ;;
  esac
done

BATCH_SLUG="$(slugify "$BATCH_RAW")"
[[ -n "$BATCH_SLUG" ]] || { echo "FEHLER: leerer Slug: $BATCH_RAW" >&2; exit 1; }

DEST="${CONSUME_LEGACY_ROOT%/}/${BATCH_SLUG}"
mkdir -p "$DEST"
[[ -n "${LEGACY_IMPORTED_LIST:-}" ]] && mkdir -p "$(dirname "$LEGACY_IMPORTED_LIST")" && touch "$LEGACY_IMPORTED_LIST"

if [[ "$DRY_RUN" -eq 0 ]]; then
  shopt -s nullglob
  orphans=( "$DEST"/*.pdf.json "$DEST"/*/*.pdf.json )
  [[ ${#orphans[@]} -gt 0 ]] && rm -f "${orphans[@]}"
  shopt -u nullglob
fi

mapfile -d '' PDFS < <(find "$SRC" -type f \( -iname '*.pdf' \) -print0 | sort -z)
TOTAL="${#PDFS[@]}"
[[ "$TOTAL" -gt 0 ]] || { echo "Keine PDFs unter $SRC"; exit 1; }

PENDING=()
for pdf in "${PDFS[@]}"; do
  rel="${pdf#$SRC/}"
  rel="${rel#/}"
  already_imported "$rel" && continue
  dest_pdf="$DEST/$rel"
  [[ -f "$dest_pdf" ]] && continue
  PENDING+=("$pdf")
done

if [[ ${#PENDING[@]} -eq 0 ]]; then
  echo "Keine ausstehenden PDFs für $BATCH_SLUG ($TOTAL gesamt, alle erledigt)"
  exit 2
fi

if [[ "$CHUNK" -gt 0 && ${#PENDING[@]} -gt "$CHUNK" ]]; then
  PENDING=("${PENDING[@]:0:$CHUNK}")
  echo "Chunk: ${#PENDING[@]} PDFs (max $CHUNK)"
elif [[ "$LIMIT" -gt 0 && ${#PENDING[@]} -gt "$LIMIT" ]]; then
  PENDING=("${PENDING[@]:0:$LIMIT}")
  echo "Limit: ${#PENDING[@]} PDFs"
fi

echo "==> Quelle:  $SRC"
echo "==> Ziel:    $DEST"
echo "==> Kopiere: ${#PENDING[@]} (von $TOTAL gesamt)"
echo "==> Tag:     $LEGACY_TAG"
[[ "$DRY_RUN" -eq 1 ]] && echo "==> Modus:   dry-run"

RSYNC_OPTS=(-a)
[[ "$DRY_RUN" -eq 1 ]] && RSYNC_OPTS+=(-n -v)

for pdf in "${PENDING[@]}"; do
  rel="${pdf#$SRC/}"
  rel="${rel#/}"
  dest_dir="$DEST/$(dirname "$rel")"
  dest_pdf="$DEST/$rel"
  [[ "$DRY_RUN" -eq 0 ]] && mkdir -p "$dest_dir"
  echo "→ $(basename "$pdf")"
  rsync "${RSYNC_OPTS[@]}" "$pdf" "$dest_pdf"
  if [[ "$DRY_RUN" -eq 0 && -n "${LEGACY_LAST_CHUNK_FILE:-}" ]]; then
    printf '%s\n' "$rel" >>"$LEGACY_LAST_CHUNK_FILE"
  fi
done

echo "COPIED: ${#PENDING[@]}"
exit 0
