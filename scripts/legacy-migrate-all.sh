#!/usr/bin/env bash
# Legacy-Migration: alle Batches nacheinander, mit Fortschritts-Log und Auto-Skip bei Hängern.
#
#   ./scripts/legacy-migrate-all.sh              # voller Lauf
#   ./scripts/legacy-migrate-all.sh --status     # Übersicht
#   ./scripts/legacy-migrate-all.sh --from lohn  # ab Batch (cs/policen überspringen)
#   ./scripts/legacy-migrate-all.sh --cleanup-consume   # hängende PDFs aus consume räumen
#   ./scripts/legacy-migrate-all.sh --retry-skipped     # skipped/ erneut importieren
#   ./scripts/legacy-migrate-all.sh --with-retry        # voller Lauf + Retry am Ende
#
# State:  /mnt/paperless-data/legacy-migrate/state.tsv
# Log:    /mnt/paperless-data/legacy-migrate/migrate.log
# Skip:   /mnt/paperless-data/legacy-migrate/skipped/<batch>/  (NICHT unter consume/)
#
set -euo pipefail

NAS_ROOT="${LEGACY_NAS_FINANZEN:-/mnt/nas-legacy/Eltern/Finanzen}"
IMPORT_SH="${LEGACY_IMPORT_SH:-/opt/paperless-scripts/legacy-import-batch.sh}"
CONSUME_ROOT="${LEGACY_CONSUME_ROOT:-/mnt/paperless-data/consume/legacy}"
STATE_DIR="${LEGACY_MIGRATE_STATE_DIR:-/mnt/paperless-data/legacy-migrate}"
SKIP_ROOT="${LEGACY_SKIP_ROOT:-$STATE_DIR/skipped}"
OLD_SKIP_ROOT="${LEGACY_OLD_SKIP_ROOT:-$(dirname "$CONSUME_ROOT")/_skipped}"
STATE_FILE="$STATE_DIR/state.tsv"
SKIPPED_TSV="$STATE_DIR/skipped.tsv"
LOG_FILE="$STATE_DIR/migrate.log"
ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"
COMPOSE_FILE="${PAPERLESS_COMPOSE:-/opt/paperless/docker-compose.yml}"

STALL_SLEEP="${LEGACY_STALL_SLEEP:-30}"       # Sekunden zwischen Prüfungen
STALL_CYCLES="${LEGACY_STALL_CYCLES:-10}"     # 10×30s = 5 min ohne Fortschritt → skipped
FAST_STALL_CYCLES="${LEGACY_FAST_STALL_CYCLES:-3}"  # 3×30s + Duplikat-Logs → sofort skipped
DUPLOG_SINCE="${LEGACY_DUPLOG_SINCE:-3m}"
DUPLICATE_DELETE_KEY="PAPERLESS_CONSUMER_DELETE_DUPLICATES"
DUPLICATE_DELETE_BACKUP="__unset__"

FROM_SLUG=""
FROM_ACTIVE=0
MARK_DONE=""
DRY_RUN=0
WITH_RETRY=0
DO_CLEANUP=0
DO_RETRY=0
RETRY_BATCH=""
MIGRATION_ACTIVE=0

mkdir -p "$STATE_DIR" "$SKIP_ROOT"

if [[ ! -f "$STATE_FILE" ]]; then
  printf '%s\n' 'slug	status	source	expected	legacy_before	legacy_after	skipped	started	finished' >"$STATE_FILE"
fi
if [[ ! -f "$SKIPPED_TSV" ]]; then
  printf '%s\n' 'timestamp	batch	file	reason' >"$SKIPPED_TSV"
fi

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  echo ""
  echo "Optionen:"
  echo "  --status              Übersicht"
  echo "  --from <slug>         Ab Batch starten"
  echo "  --mark-done <a,b>     Batches als erledigt markieren"
  echo "  --cleanup-consume     Hänger aus consume/legacy + altes consume/_skipped räumen"
  echo "  --retry-skipped [batch]  PDFs aus skipped/ erneut in consume legen"
  echo "  --with-retry          Voller Lauf, danach --retry-skipped"
  echo "  --dry-run | -h"
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

consumer_logs() {
  [[ -f "$COMPOSE_FILE" ]] || return 0
  docker compose -f "$COMPOSE_FILE" logs webserver --since "$DUPLOG_SINCE" 2>&1 || true
}

# Alle verbleibenden PDFs eines Batches erscheinen als Duplikat in den Paperless-Logs?
files_all_duplicate_in_logs() {
  local slug="$1"
  local dir="$CONSUME_ROOT/$slug"
  local logs total=0 dup=0 f base

  logs=$(consumer_logs)
  while IFS= read -r -d '' f; do
    total=$((total + 1))
    base=$(basename "$f")
    if echo "$logs" | grep -qF "Not consuming ${base}:"; then
      dup=$((dup + 1))
    fi
  done < <(find "$dir" -type f \( -iname '*.pdf' \) -print0 2>/dev/null)

  [[ "$total" -gt 0 && "$dup" -eq "$total" ]]
}

record_skip() {
  local batch="$1" relpath="$2" reason="$3"
  printf '%s\t%s\t%s\t%s\n' "$(date '+%F %T')" "$batch" "$relpath" "$reason" >>"$SKIPPED_TSV"
}

# Altes consume/_skipped → legacy-migrate/skipped/ (Paperless scannt consume rekursiv!)
migrate_old_skip_folder() {
  [[ -d "$OLD_SKIP_ROOT" ]] || return 0
  local n=0 f rel dest
  while IFS= read -r -d '' f; do
    rel="${f#$OLD_SKIP_ROOT/}"
    dest="$SKIP_ROOT/$rel"
    mkdir -p "$(dirname "$dest")"
    if [[ -e "$dest" ]]; then
      rm -f "$f"
    else
      mv "$f" "$dest"
    fi
    n=$((n + 1))
  done < <(find "$OLD_SKIP_ROOT" -type f \( -iname '*.pdf' \) -print0 2>/dev/null)
  if [[ "$n" -gt 0 ]]; then
    log "MIGRATE old skip: $n PDFs von $OLD_SKIP_ROOT → $SKIP_ROOT"
  fi
  rmdir -p "$OLD_SKIP_ROOT" 2>/dev/null || true
}

skip_remaining_pdfs() {
  local slug="$1" reason="$2"
  local dir="$CONSUME_ROOT/$slug"
  local dest="$SKIP_ROOT/$slug"
  local n=0 f rel dest_file

  mkdir -p "$dest"
  while IFS= read -r -d '' f; do
    n=$((n + 1))
    rel="${f#$dir/}"
    rel="${rel#/}"
    dest_file="$dest/$rel"
    mkdir -p "$(dirname "$dest_file")"
    mv "$f" "$dest_file"
    record_skip "$slug" "$rel" "$reason"
    log "SKIP $slug: $rel ($reason)"
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
      if [[ "$idle" -ge "$FAST_STALL_CYCLES" ]] && files_all_duplicate_in_logs "$slug"; then
        log "FAST-SKIP $slug: $n PDFs — alle Duplikate laut Logs → $SKIP_ROOT"
        skip_remaining_pdfs "$slug" "duplicate"
        break
      fi
      if [[ "$idle" -ge "$STALL_CYCLES" ]]; then
        log "STALL $slug: $n PDFs seit $((STALL_CYCLES * STALL_SLEEP))s unverändert → $SKIP_ROOT"
        skip_remaining_pdfs "$slug" "stall"
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

enable_delete_duplicates() {
  [[ "${LEGACY_DELETE_DUPLICATES:-1}" == "0" ]] && return 0
  [[ -f "$ENV_FILE" ]] || { log "WARN: $ENV_FILE fehlt — DELETE_DUPLICATES nicht gesetzt"; return 0; }

  local current=""
  if grep -q "^${DUPLICATE_DELETE_KEY}=" "$ENV_FILE"; then
    current=$(grep -m1 "^${DUPLICATE_DELETE_KEY}=" "$ENV_FILE" | cut -d= -f2-)
  else
    current="false"
  fi

  if [[ "$current" == "true" ]]; then
    DUPLICATE_DELETE_BACKUP=""
    return 0
  fi

  DUPLICATE_DELETE_BACKUP="$current"
  if grep -q "^${DUPLICATE_DELETE_KEY}=" "$ENV_FILE"; then
    sed -i "s/^${DUPLICATE_DELETE_KEY}=.*/${DUPLICATE_DELETE_KEY}=true/" "$ENV_FILE"
  else
    printf '\n%s=true\n' "$DUPLICATE_DELETE_KEY" >>"$ENV_FILE"
  fi
  log "SET ${DUPLICATE_DELETE_KEY}=true (Duplikate werden aus consume entfernt)"

  if [[ -f "$COMPOSE_FILE" ]] && command -v docker >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" up -d --force-recreate webserver
    log "Paperless webserver neu erstellt (DELETE_DUPLICATES aktiv)"
  fi
}

restore_delete_duplicates() {
  [[ "$MIGRATION_ACTIVE" -eq 0 ]] && return 0
  [[ "$DUPLICATE_DELETE_BACKUP" == "__unset__" ]] && return 0
  [[ -z "$DUPLICATE_DELETE_BACKUP" ]] && return 0
  [[ -f "$ENV_FILE" ]] || return 0

  sed -i "s/^${DUPLICATE_DELETE_KEY}=.*/${DUPLICATE_DELETE_KEY}=${DUPLICATE_DELETE_BACKUP}/" "$ENV_FILE"
  log "RESTORE ${DUPLICATE_DELETE_KEY}=${DUPLICATE_DELETE_BACKUP}"

  if [[ -f "$COMPOSE_FILE" ]] && command -v docker >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" up -d --force-recreate webserver
  fi
}

migration_trap_exit() {
  local rc=$?
  restore_delete_duplicates
  exit "$rc"
}

# Hängende PDFs in consume/legacy/<batch>/ → skipped/; altes consume/_skipped migrieren
cleanup_consume() {
  migrate_old_skip_folder
  local total=0 n slug d

  for d in "$CONSUME_ROOT"/*/; do
    [[ -d "$d" ]] || continue
    slug=$(basename "$d")
    [[ "$slug" == "_retry" ]] && continue
    n=$(count_consume_pdfs "$slug")
    [[ "$n" -eq 0 ]] && continue
    log "CLEANUP $slug: $n PDFs → $SKIP_ROOT"
    n=$(skip_remaining_pdfs "$slug" "cleanup")
    total=$((total + n))
    rm -rf "$d"
  done

  log "CLEANUP fertig: $total PDFs nach $SKIP_ROOT"
  print_status
}

# PDFs aus skipped/ erneut importieren (optional nur ein Batch)
retry_skipped() {
  local filter="${1:-}"
  migrate_old_skip_folder

  local retry_slug="_retry"
  local dest="$CONSUME_ROOT/$retry_slug"
  local n=0 f rel batch subpath src

  rm -rf "$dest"
  mkdir -p "$dest"

  while IFS= read -r -d '' f; do
    rel="${f#$SKIP_ROOT/}"
    batch="${rel%%/*}"
    [[ -n "$filter" && "$batch" != "$filter" ]] && continue
    subpath="${rel#*/}"
    src="$dest/$batch/$subpath"
    mkdir -p "$(dirname "$src")"
    cp -f "$f" "$src"
    n=$((n + 1))
    log "RETRY queue: $batch/$subpath"
  done < <(find "$SKIP_ROOT" -type f \( -iname '*.pdf' \) -print0 2>/dev/null | sort -z)

  if [[ "$n" -eq 0 ]]; then
    log "RETRY: keine PDFs in $SKIP_ROOT${filter:+ (batch=$filter)}"
    return 0
  fi

  log "RETRY: $n PDFs → consume/legacy/$retry_slug/ (Originale bleiben in $SKIP_ROOT)"
  MIGRATION_ACTIVE=1
  trap migration_trap_exit EXIT
  enable_delete_duplicates

  local lb la started finished sk
  lb=$(legacy_api_count)
  started=$(date '+%F %T')
  wait_consume_batch "$retry_slug"
  la=$(legacy_api_count)
  finished=$(date '+%F %T')
  sk=$(awk -F'\t' -v b="$retry_slug" '$2==b {c++} END{print c+0}' "$SKIPPED_TSV")
  record_state "$retry_slug" "retry" "$SKIP_ROOT" "$n" "$lb" "$la" "$sk" "$started" "$finished"
  log "RETRY fertig | neu legacy: $((la == "?" || lb == "?" ? -1 : la - lb)) | erneut skipped: $sk"
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
  echo "Skip:  $SKIP_ROOT"
  echo ""
  column -t -s $'\t' "$STATE_FILE" 2>/dev/null || cat "$STATE_FILE"
  echo ""
  local total_sk skip_pdfs
  total_sk=$(awk -F'\t' 'NR>1{c++} END{print c+0}' "$SKIPPED_TSV")
  skip_pdfs=$(find "$SKIP_ROOT" -type f \( -iname '*.pdf' \) 2>/dev/null | wc -l | tr -d ' ')
  echo "Skipped-Einträge (skipped.tsv): $total_sk"
  echo "PDFs in $SKIP_ROOT: $skip_pdfs"
  local consume_left
  consume_left=$(find "$CONSUME_ROOT" -type f \( -iname '*.pdf' \) 2>/dev/null | wc -l | tr -d ' ')
  echo "PDFs noch in consume/legacy: $consume_left"
  if [[ -d "$OLD_SKIP_ROOT" ]] && find "$OLD_SKIP_ROOT" -type f \( -iname '*.pdf' \) 2>/dev/null | grep -q .; then
    echo "WARN: altes $OLD_SKIP_ROOT noch belegt — --cleanup-consume ausführen"
  fi
  echo "Legacy-Doks jetzt: $(legacy_api_count)"
  echo ""
  local nas_total
  nas_total=$(find "$NAS_ROOT" -type f \( -iname '*.pdf' \) \
    ! -path '*/Vorsorge/Moni/2015/*' ! -path '*/Vorsorge/Moni/2016/*' 2>/dev/null | wc -l | tr -d ' ')
  echo "NAS Inventur (ohne Moni 2015/2016): $nas_total PDFs"
  echo "Log: $LOG_FILE"
  echo ""
  echo "Nachholen: $0 --retry-skipped"
}

# --- Batch-Liste (Reihenfolge) ---
run_all_batches() {
  migrate_old_skip_folder
  MIGRATION_ACTIVE=1
  trap migration_trap_exit EXIT
  enable_delete_duplicates

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
  if [[ "$WITH_RETRY" -eq 1 ]]; then
    log "========== RETRY SKIPPED =========="
    retry_skipped
  fi
  print_status
}

# --- CLI ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --status) print_status; exit 0 ;;
    --from) FROM_SLUG="${2:?}"; shift 2 ;;
    --mark-done) MARK_DONE="${2:?}"; shift 2 ;;
    --cleanup-consume) DO_CLEANUP=1; shift ;;
    --retry-skipped)
      DO_RETRY=1
      if [[ "${2:-}" != "" && "${2:0:1}" != "-" ]]; then
        RETRY_BATCH="$2"
        shift
      fi
      shift
      ;;
    --with-retry) WITH_RETRY=1; shift ;;
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

if [[ "$DO_CLEANUP" -eq 1 ]]; then
  exec >>"$LOG_FILE" 2>&1
  log "=== --cleanup-consume (PID $$) ==="
  cleanup_consume
  exit 0
fi

if [[ "$DO_RETRY" -eq 1 ]]; then
  exec >>"$LOG_FILE" 2>&1
  log "=== --retry-skipped (PID $$) batch=${RETRY_BATCH:-all} ==="
  retry_skipped "$RETRY_BATCH"
  print_status
  exit 0
fi

exec >>"$LOG_FILE" 2>&1
log "=== legacy-migrate-all.sh start (PID $$) ==="
run_all_batches
