#!/usr/bin/env bash
# Paperless-NGX 2.20.15 → 3.1.2 auf CT 121 (Produktion).
# Voraussetzung: PBS-Snapshot + Legacy fertig (UPGRADE_V3.md Phase 0+1).
#
# Aufruf:
#   cd /opt/paperless-ngx-classifier && git pull
#   ./scripts/deploy-to-ct121.sh --no-docker
#   ./scripts/upgrade-v3-ct121.sh --dry-run    # nur anzeigen
#   ./scripts/upgrade-v3-ct121.sh                # ausführen
set -euo pipefail

COMPOSE_DIR="${PAPERLESS_COMPOSE_DIR:-/opt/paperless}"
TARGET_IMAGE="ghcr.io/paperless-ngx/paperless-ngx:3.1.2"
DRY=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
  esac
done

compose_file="$COMPOSE_DIR/docker-compose.yml"
env_file="$COMPOSE_DIR/.env"

[[ -f "$compose_file" ]] || { echo "FEHLER: $compose_file fehlt" >&2; exit 1; }
[[ -f "$env_file" ]]     || { echo "FEHLER: $env_file fehlt" >&2; exit 1; }

echo "=== Paperless v3 Upgrade (CT 121) ==="
echo "Compose: $compose_file"
echo "Ziel:    $TARGET_IMAGE"
echo ""

if pgrep -af 'legacy-migrate|legacy-import-loop' >/dev/null 2>&1; then
  echo "FEHLER: Legacy-Import läuft noch — abbrechen." >&2
  exit 1
fi

cid="$(docker ps -qf 'name=webserver' 2>/dev/null | head -1 || true)"
if [[ -n "$cid" ]]; then
  cur="$(docker exec "$cid" python3 -c "
from pathlib import Path
p = Path('/usr/src/paperless/src/paperless/version.py')
ns = {}
exec(p.read_text(), ns)
print('.'.join(map(str, ns['__version__'])))
" 2>/dev/null || true)"
  echo "Aktuelle App-Version: ${cur:-unbekannt}"
  if [[ -n "$cur" && "$cur" != "2.20.15" && "$cur" != 3.* ]]; then
    echo "WARN: Upgrade-Pfad ist nur von 2.20.15 — trotzdem fortfahren? (Ctrl+C)" >&2
    sleep 5
  fi
fi

backup_ts="$(date +%Y%m%d-%H%M%S)"
echo "Backups: docker-compose.yml.bak-v2-$backup_ts, .env.bak-v2-$backup_ts"

patch_env() {
  local f="$1"
  cp "$f" "${f}.bak-v2-$backup_ts"
  sed -i 's/^PAPERLESS_OCR_MODE=skip/PAPERLESS_OCR_MODE=auto/' "$f"
  sed -i '/^PAPERLESS_OCR_SKIP_ARCHIVE_FILE=/d' "$f"
  sed -i '/^PAPERLESS_CONSUMER_POLLING=/d' "$f"
  sed -i '/^CONSUMER_BARCODE_SCANNER=/d' "$f"
  grep -q '^PAPERLESS_ARCHIVE_FILE_GENERATION=' "$f" || \
    echo 'PAPERLESS_ARCHIVE_FILE_GENERATION=always' >> "$f"
  grep -q '^PAPERLESS_CONSUMER_POLLING_INTERVAL=' "$f" || \
    echo 'PAPERLESS_CONSUMER_POLLING_INTERVAL=10' >> "$f"
  grep -q '^PAPERLESS_DBENGINE=' "$f" || \
    echo 'PAPERLESS_DBENGINE=postgresql' >> "$f"
  grep -q '^PAPERLESS_AI_ENABLED=' "$f" || \
    echo 'PAPERLESS_AI_ENABLED=false' >> "$f"
  grep -q '^PAPERLESS_CONSUMER_DELETE_DUPLICATES=' "$f" || \
    echo 'PAPERLESS_CONSUMER_DELETE_DUPLICATES=true' >> "$f"
}

patch_compose() {
  local f="$1"
  cp "$f" "${f}.bak-v2-$backup_ts"
  sed -i "s|ghcr.io/paperless-ngx/paperless-ngx:[^[:space:]]*|$TARGET_IMAGE|g" "$f"
  if ! grep -q 'PAPERLESS_DBENGINE' "$f"; then
    sed -i '/PAPERLESS_DBHOST:/i\      PAPERLESS_DBENGINE: postgresql' "$f"
  fi
}

echo ""
echo "--- .env Änderungen (geplant) ---"
grep -E '^(PAPERLESS_OCR_MODE|PAPERLESS_ARCHIVE|PAPERLESS_CONSUMER|PAPERLESS_DBENGINE|PAPERLESS_AI)' "$env_file" || true
echo ""
echo "--- compose image (aktuell) ---"
grep 'paperless-ngx' "$compose_file" || true

if [[ "$DRY" -eq 1 ]]; then
  echo ""
  echo "DRY-RUN — keine Änderungen. Start ohne --dry-run zum Ausführen."
  exit 0
fi

read -r -p "Fortfahren mit Upgrade? [y/N] " ans
[[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]] || { echo "Abgebrochen."; exit 0; }

patch_env "$env_file"
patch_compose "$compose_file"

echo ""
echo "==> docker compose pull webserver"
(cd "$COMPOSE_DIR" && docker compose pull webserver)

echo "==> docker compose up -d --force-recreate webserver"
(cd "$COMPOSE_DIR" && docker compose up -d --force-recreate webserver)

echo ""
echo "==> Logs (Ctrl+C beendet nur tail — Container läuft weiter):"
echo "    cd $COMPOSE_DIR && docker compose logs -f webserver"
echo ""
echo "==> Nach 'document management system ready':"
echo "    cd /opt/paperless-ngx-classifier && ./scripts/paperless-version-check.sh"
echo "    ./scripts/deploy-to-ct121.sh"
echo ""
echo "Smoke-Tests: docs/UPGRADE_V3.md Phase 5"
