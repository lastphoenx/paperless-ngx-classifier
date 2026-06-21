#!/usr/bin/env bash
# Legacy-Altbestand: NAS-Ordner → consume/legacy/<batch>/ (Tags setzt post_consume per API)
#
# Aufruf (auf dem Paperless-Host CT 121, nach NFS-Mount):
#   ./scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen eltern-finanzen --limit 10
#   ./scripts/legacy-import-batch.sh /mnt/nas-legacy/Thomas/Finanzen thomas-finanzen --dry-run
#
# Voraussetzungen:
#   - NAS lesbar auf CT 121 (typisch: NFS /mnt/nas-legacy → pi-nas:/srv/nas) — siehe docs/LEGACY_IMPORT.md
#   - Tag "legacy" in Paperless angelegt
#   - LEGACY_CONSUME_MARKERS=/legacy/ in /opt/paperless/.env + Container recreate
#   - PAPERLESS_CONSUMER_RECURSIVE=true, PAPERLESS_OCR_MODE=skip
#
set -euo pipefail

LEGACY_TAG="${LEGACY_TAG:-legacy}"
CONSUME_LEGACY_ROOT="${LEGACY_CONSUME_ROOT:-/mnt/paperless-data/consume/legacy}"
DRY_RUN=0
LIMIT=0

usage() {
  cat <<'EOF'
Usage: legacy-import-batch.sh <NAS-QUELLPFAD> <BATCH-NAME> [Optionen]

Argumente:
  NAS-QUELLPFAD   z. B. /mnt/nas-legacy/Eltern/Finanzen (CT 121 nach NFS-Mount)
  BATCH-NAME      Ziel unter consume/legacy/<batch>/ (nur a-z0-9 und -)

Optionen:
  --limit N       Nur die ersten N PDFs kopieren (Testlauf)
  --dry-run       Zeigt rsync an, schreibt nichts
  -h, --help      Diese Hilfe

Umgebungsvariablen:
  LEGACY_CONSUME_ROOT   Standard: /mnt/paperless-data/consume/legacy
  LEGACY_TAG            Standard: legacy (muss in Paperless existieren)

Nach dem Kopieren: Paperless verarbeitet consume/legacy/ automatisch.
Originale auf dem NAS bleiben unverändert (nur Kopie).
EOF
}

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g'
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
  echo "Hinweis: Auf CT 121 typisch NFS-Mount /mnt/nas-legacy — siehe docs/LEGACY_IMPORT.md" >&2
  exit 1
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) LIMIT="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; usage; exit 1 ;;
  esac
done

BATCH_SLUG="$(slugify "$BATCH_RAW")"
if [[ -z "$BATCH_SLUG" ]]; then
  echo "FEHLER: BATCH-NAME ergibt leeren Slug: $BATCH_RAW" >&2
  exit 1
fi

DEST="${CONSUME_LEGACY_ROOT%/}/${BATCH_SLUG}"
mkdir -p "$DEST"

# Alte .pdf.json-Waisen (frühere Script-Version) — Paperless liest sie nicht, erzeugt nur Log-Warnungen
if [[ "$DRY_RUN" -eq 0 ]]; then
  shopt -s nullglob
  orphans=( "$DEST"/*.pdf.json "$DEST"/*/*.pdf.json )
  if [[ ${#orphans[@]} -gt 0 ]]; then
    echo "==> Entferne ${#orphans[@]} alte Sidecar-Waisen (*.pdf.json)"
    rm -f "${orphans[@]}"
  fi
  shopt -u nullglob
fi

mapfile -d '' PDFS < <(find "$SRC" -type f \( -iname '*.pdf' \) -print0 | sort -z)
TOTAL="${#PDFS[@]}"

if [[ "$TOTAL" -eq 0 ]]; then
  echo "Keine PDFs unter $SRC"
  exit 1
fi

if [[ "$LIMIT" -gt 0 && "$TOTAL" -gt "$LIMIT" ]]; then
  PDFS=("${PDFS[@]:0:$LIMIT}")
  echo "Limit aktiv: ${#PDFS[@]} von $TOTAL PDFs"
fi

echo "==> Quelle:  $SRC"
echo "==> Ziel:    $DEST"
echo "==> PDFs:    ${#PDFS[@]}"
echo "==> Tag:     $LEGACY_TAG (Speicherpfad: legacy/{title} via post_consume)"
[[ "$DRY_RUN" -eq 1 ]] && echo "==> Modus:   dry-run"

RSYNC_OPTS=(-a --ignore-existing)
[[ "$DRY_RUN" -eq 1 ]] && RSYNC_OPTS+=(-n -v)

for pdf in "${PDFS[@]}"; do
  rel="${pdf#$SRC/}"
  rel="${rel#/}"
  dest_dir="$DEST/$(dirname "$rel")"
  dest_pdf="$DEST/$rel"

  if [[ "$DRY_RUN" -eq 0 ]]; then
    mkdir -p "$dest_dir"
  fi

  echo "→ $(basename "$pdf")"
  rsync "${RSYNC_OPTS[@]}" "$pdf" "$dest_pdf"
done

echo ""
echo "Fertig. Paperless consume prüft alle ${PAPERLESS_CONSUMER_POLLING:-10}s."
echo "Logs: docker compose -f /opt/paperless/docker-compose.yml logs -f webserver | grep -i legacy"
echo "Stichprobe: Paperless → Filter Tag=$LEGACY_TAG"
