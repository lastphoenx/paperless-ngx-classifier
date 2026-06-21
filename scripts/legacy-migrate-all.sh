#!/usr/bin/env bash
# Legacy-Migration: alle Batches nacheinander, mit Fortschritts-Log und Auto-Skip bei Hängern.
#
#   ./scripts/legacy-migrate-all.sh              # voller Lauf
#   ./scripts/legacy-migrate-all.sh --status   # Übersicht
#   ./scripts/legacy-migrate-all.sh --from lohn  # ab Batch (cs/policen überspringen)
#
# State:  /mnt/paperless-data/legacy-migrate/state.tsv
# Log:    /mnt/paperless-data/legacy-migrate/migrate.log
# Skip:   /mnt/paperless-data/consume/_skipped/<batch>/ + skipped.tsv
#
set -euo pipefail

NAS_ROOT="${LEGACY_NAS_FINANZEN:-/mnt/nas-legacy/Eltern/Finanzen}"
IMPORT_SH="${LEGACY_IMPORT_SH:-/opt/paperless-scripts/legacy-import-batch.sh}"
CONSUME_ROOT="${LEGACY_CONSUME_ROOT:-/mnt/paperless-data/consume/legacy}"
STATE_DIR="${LEGACY_MIGRATE_STATE_DIR:-/mnt/paperless-data/legacy-migrate}"
STATE_FILE="$STATE_DIR/state.tsv"
SKIPPED_TSV="$STATE_DIR/skipped.tsv"
LOG_FILE="$STATE_DIR/migrate.log"
ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"

STALL_SLEEP="${LEGACY_STALL_SLEEP:-30}"       # Sekunden zwischen Prüfungen
STALL_CYCLES="${LEGACY_STALL_CYCLES:-10}"     # 10×30s = 5 min ohne Fortschritt → _skipped

FROM_SLUG=""
FROM_ACTIVE=0
MARK_DONE=""
DRY_RUN=0

mkdir -p "$STATE_DIR" "$(dirname "$CONSUME_ROOT")/_skipped"

if [[ ! -f "$STATE_FILE" ]]; then
  printf '%s\n' 'slug	status	source	expected	legacy_before	legacy_after	skipped	started	finished' >"$STATE_FILE"
fi
if [[ ! -f "$SKIPPED_TSV" ]]; then
  printf '%s\n' 'timestamp	batch	file	reason' >"$SKIPPED_TSV"
fi

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  echo ""
  echo "Optionen: --status | --from <slug> | --dry-run | -h"
}

count_pdfs() {
  find "$1" -type f \( -iname '*.pdf' \) 2>/dev/null | wc -l | tr -d ' '
}

count_consume_pdfs() {
  local dir="$CONSUME_ROOT/$1"
  [[ -d "$dir" ]] || { echo 0; return; }
  find "$dir" -type f \( -iname '*.pdf' \) 2>/dev/null | wc -l | tr -d ' '
}

legacy_api_count() {
  local token
  token=$(grep -m1 '^PAPERLESS_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
  [[ -n "$token" ]] || { echo "?"; return; }
  curl -sf -H "Authorization: Token $token" \
    'http://127.0.0.1:8000/api/documents/?tags__name__iexact=legacy&page_size=1' \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('count','?'))" 2>/dev/null || echo "?"
}

batch_done() {
  local slug="$1"
  awk -F'\t' -v s="$slug" '$1==s && $2=="done" {found=1} END{exit !found}' "$STATE_FILE"
}

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"
}

record_state() {
  local slug="$1" status="$2" source="$3" expected="$4" lb="$5" la="$6" skipped="$7" started="$8" finished="$9"
  local tmp
  tmp=$(mktemp)
  awk -F'\t' -v s="$slug" '$1!=s' "$STATE_FILE" >"$tmp"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$slug" "$status" "$source" "$expected" "$lb" "$la" "$skipped" "$started" "$finished" >>"$tmp"
  mv "$tmp" "$STATE_FILE"
}

skip_remaining_pdfs() {
  local slug="$1" reason="$2"
  local dir="$CONSUME_ROOT/$slug"
  local dest="$(dirname "$CONSUME_ROOT")/_skipped/$slug"
  local n=0 f
  mkdir -p "$dest"
  while IFS= read -r -d '' f; do
  n=$((n + 1))
  mv "$f" "$dest/"
  printf '%s\t%s\t%s\t%s\n' "$(date '+%F %T')" "$slug" "$(basename "$f")" "$reason" >>"$SKIPPED_TSV"
  log "SKIP $slug: $(basename "$f") ($reason)"
  done < <(find "$dir" -type f \( -iname '*.pdf' \) -print0 2>/dev/null)
  echo "$n"
}

wait_consume_batch() {
  local slug="$1"
  local dir="$CONSUME_ROOT/$slug"
  local idle=0 last=-1 n=0

  log "WAIT $slug: consume prüfen …"
  while true; do
    n=$(count_consume_pdfs "$slug")
    [[ "$n" -eq 0 ]] && break
    if [[ "$n" -eq "$last" ]]; then
      idle=$((idle + 1))
      if [[ "$idle" -ge "$STALL_CYCLES" ]]; then
        log "STALL $slug: $n PDFs seit $((STALL_CYCLES * STALL_SLEEP))s unverändert → _skipped"
        skip_remaining_pdfs "$slug" "stall/duplicate"
        break
      fi
    else
      idle=0
      last=$n
    fi
    log "WAIT $slug: noch $n PDFs"
    sleep "$STALL_SLEEP"
  done
  rm -rf "$dir"
  log "DONE consume $slug"
}

run_batch() {
  local src="$1" slug="$2"
  local expected lb la skipped_n started finished sk

  if batch_done "$slug"; then
    log "SKIP batch $slug (bereits done laut state.tsv)"
    return 0
  fi

  if [[ -n "$FROM_SLUG" && "$FROM_ACTIVE" -eq 0 ]]; then
    if [[ "$slug" != "$FROM_SLUG" ]]; then
      log "SKIP batch $slug (vor --from $FROM_SLUG)"
      return 0
    fi
    FROM_ACTIVE=1
  fi

  [[ -d "$src" ]] || { log "FEHLER: Quelle fehlt: $src"; return 1; }
  expected=$(count_pdfs "$src")
  lb=$(legacy_api_count)
  started=$(date '+%F %T')

  log "START $slug | Quelle: $src | PDFs: $expected | legacy vorher: $lb"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN $slug — kein rsync"
    return 0
  fi

  "$IMPORT_SH" "$src" "$slug"
  wait_consume_batch "$slug"

  la=$(legacy_api_count)
  sk=$(awk -F'\t' -v b="$slug" '$2==b {c++} END{print c+0}' "$SKIPPED_TSV")
  finished=$(date '+%F %T')
  record_state "$slug" "done" "$src" "$expected" "$lb" "$la" "$sk" "$started" "$finished"

  local imported=$((la == "?" || lb == "?" ? -1 : la - lb))
  log "END $slug | erwartet: $expected | neu legacy: $imported | skipped: $sk | legacy gesamt: $la"
}

print_status() {
  echo ""
  echo "=== Legacy-Migration Status ==="
  echo "State: $STATE_FILE"
  echo ""
  column -t -s $'\t' "$STATE_FILE" 2>/dev/null || cat "$STATE_FILE"
  echo ""
  local total_sk
  total_sk=$(awk -F'\t' 'NR>1{c++} END{print c+0}' "$SKIPPED_TSV")
  echo "Skipped-Dateien gesamt: $total_sk (Details: $SKIPPED_TSV)"
  echo "Legacy-Doks jetzt: $(legacy_api_count)"
  echo ""
  local nas_total
  nas_total=$(find "$NAS_ROOT" -type f \( -iname '*.pdf' \) \
    ! -path '*/Vorsorge/Moni/2015/*' ! -path '*/Vorsorge/Moni/2016/*' 2>/dev/null | wc -l | tr -d ' ')
  echo "NAS Inventur (ohne Moni 2015/2016): $nas_total PDFs"
  echo "Log: $LOG_FILE"
}

# --- Batch-Liste (Reihenfolge) ---
run_all_batches() {
  run_batch "$NAS_ROOT/CS" cs
  run_batch "$NAS_ROOT/Policen" policen
  run_batch "$NAS_ROOT/Lohn" lohn
  run_batch "$NAS_ROOT/Fano" fano
  run_batch "$NAS_ROOT/Bestellungen" bestellungen
  run_batch "$NAS_ROOT/BLKB" blkb
  run_batch "$NAS_ROOT/Ameritrade" ameritrade
  run_batch "$NAS_ROOT/Erb_Bern" erb-bern
  run_batch "$NAS_ROOT/Erbschaft_Gassacker" erbschaft-gassacker

  local y base sub name slug
  for y in "$NAS_ROOT/Vorsorge/Moni"/*/; do
    [[ -d "$y" ]] || continue
    base=$(basename "$y")
    [[ "$base" == "2015" || "$base" == "2016" ]] && continue
    run_batch "$y" "vorsorge-moni-$base"
  done
  for sub in "$NAS_ROOT/Vorsorge"/*/; do
    [[ -d "$sub" ]] || continue
    [[ "$(basename "$sub")" == "Moni" ]] && continue
    run_batch "$sub" "vorsorge-$(basename "$sub" | tr '[:upper:]' '[:lower:]')"
  done

  run_batch "$NAS_ROOT/Steuern" steuern
  run_batch "$NAS_ROOT/Rechnungen" rechnungen

  local SKIP_RE='^(CS|Policen|Lohn|Fano|Bestellungen|BLKB|Ameritrade|Erb_Bern|Erbschaft_Gassacker|Steuern|Rechnungen|Vorsorge)$'
  for d in "$NAS_ROOT"/*/; do
    name=$(basename "$d")
    [[ "$name" =~ $SKIP_RE ]] && continue
    slug=$(echo "$name" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g')
    run_batch "$d" "$slug"
  done

  # Wurzel-PDFs
  if find "$NAS_ROOT" -maxdepth 1 -name '*.pdf' | grep -q .; then
    slug=finanzen-root
    if ! batch_done "$slug"; then
      mkdir -p "$CONSUME_ROOT/$slug"
      find "$NAS_ROOT" -maxdepth 1 -name '*.pdf' -exec cp -n {} "$CONSUME_ROOT/$slug/" \;
      lb=$(legacy_api_count); started=$(date '+%F %T')
      wait_consume_batch "$slug"
      la=$(legacy_api_count)
      record_state "$slug" "done" "$NAS_ROOT/*.pdf" "$(find "$NAS_ROOT" -maxdepth 1 -name '*.pdf' | wc -l)" "$lb" "$la" 0 "$started" "$(date '+%F %T')"
    fi
  fi

  log "========== MIGRATION FERTIG =========="
  print_status
}

# --- CLI ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --status) print_status; exit 0 ;;
    --from) FROM_SLUG="${2:?}"; shift 2 ;;
    --mark-done) MARK_DONE="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unbekannt: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -n "$MARK_DONE" ]]; then
  IFS=',' read -ra _marks <<<"$MARK_DONE"
  for _m in "${_marks[@]}"; do
    record_state "$_m" "done" "(manuell)" "?" "?" "$(legacy_api_count)" "?" "$(date '+%F %T')" "$(date '+%F %T')"
    echo "markiert: $_m"
  done
  print_status
  exit 0
fi

exec >>"$LOG_FILE" 2>&1
log "=== legacy-migrate-all.sh start (PID $$) ==="
run_all_batches
