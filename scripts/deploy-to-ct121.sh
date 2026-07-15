#!/usr/bin/env bash
# Deploy selected files from a local git clone to /opt/paperless-scripts on ct-121.
#
# One-time setup on ct-121:
#   git clone git@github.com:lastphoenx/paperless-ngx-classifier.git /opt/paperless-ngx-classifier
#
# Update (UI + backend + post_consume — Standard):
#   cd /opt/paperless-ngx-classifier && git pull && ./scripts/deploy-to-ct121.sh
#
# pre_consume.sh + pre_consume_qr.py + legacy-import-batch.sh werden mitdeployt.
#
# Ohne Paperless-Container-Neustart (nur Scripts + correspondent-manager):
#   ./scripts/deploy-to-ct121.sh --no-docker

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Windows-Git liefert .sh oft ohne +x — nach pull sicherstellen
chmod +x "$REPO_DIR"/scripts/*.sh 2>/dev/null || true
TARGET="${PAPERLESS_SCRIPTS_DIR:-/opt/paperless-scripts}"
PAPERLESS_COMPOSE_DIR="${PAPERLESS_COMPOSE_DIR:-/opt/paperless}"
RESTART=1
RECREATE_DOCKER=1

for arg in "$@"; do
  case "$arg" in
    --no-restart)    RESTART=0 ;;
    --no-docker)     RECREATE_DOCKER=0 ;;
  esac
done

FILES=(
  correspondent_manager_app.py
  paper_manager_ui.html
  post_consume.py
  post_consume_runner.py
  iban_utils.py
  document_date.py
  schulbericht_vision.py
  handwriting_vision.py
  image_crop.py
  htr_runner.py
  brillenpass_parser.py
  brillenpass_tsv.py
  brillenpass_runner.py
  legacy_split_by_qr.py
  legacy_qr_scan_worker.py
  phone_utils.py
  swift_utils.py
  steuerjahr.py
  pre_consume.sh
  pre_consume_qr.py
  scripts/legacy-import-batch.sh
  scripts/legacy-migrate-all.sh
  scripts/legacy-one-batch.sh
  scripts/legacy-tasks-summary.sh
  scripts/legacy-duplicate-audit.sh
  scripts/legacy-dedupe-imports.sh
  scripts/legacy-originals-audit.sh
  scripts/legacy-media-orphans.sh
  scripts/legacy-nas-sha256.sh
  scripts/legacy-prepare-pdf.sh
  scripts/backfill_dok_id.py
  scripts/repair_brillenpaesse.py
  scripts/legacy_qr_split_test.py
  scripts/test_handwriting_vision.py
  scripts/ensure-legacy-qr-deps.sh
)

echo "==> Repo:   $REPO_DIR"
echo "==> Target: $TARGET"
for f in "${FILES[@]}"; do
  src="$REPO_DIR/$f"
  dest_name="$(basename "$f")"
  [[ -f "$src" ]] || { echo "FEHLER: $src fehlt" >&2; exit 1; }
  cp -v "$src" "$TARGET/$dest_name"
  if [[ "$dest_name" == *.sh || "$dest_name" == legacy_qr_split_test.py || "$dest_name" == test_handwriting_vision.py ]]; then
    chmod +x "$TARGET/$dest_name"
  fi
done

mkdir -p "$TARGET/training"
HTR_EXAMPLE="$REPO_DIR/training/htr_profiles.example.json"
HTR_DEST="$TARGET/training/htr_profiles.json"
if [[ -f "$HTR_EXAMPLE" ]]; then
  if [[ ! -f "$HTR_DEST" ]]; then
    cp -v "$HTR_EXAMPLE" "$HTR_DEST"
    echo "==> htr_profiles.json aus Example angelegt"
  else
    echo "==> htr_profiles.json existiert bereits — nicht überschrieben"
  fi
fi

if [[ "$RESTART" -eq 1 ]] && systemctl is-active --quiet correspondent-manager 2>/dev/null; then
  echo "==> Restart correspondent-manager"
  systemctl restart correspondent-manager
fi

# post_consume/pre_consume laufen im Paperless-Container — env_file (.env) wird nur
# beim Erstellen des Containers gelesen. restart reicht nicht für neue CF_*_ID etc.
if [[ "$RECREATE_DOCKER" -eq 1 ]] && command -v docker >/dev/null 2>&1; then
  compose_file="$PAPERLESS_COMPOSE_DIR/docker-compose.yml"
  if [[ -f "$compose_file" ]]; then
    echo "==> Paperless webserver neu erstellen (lädt /opt/paperless/.env neu)"
    (cd "$PAPERLESS_COMPOSE_DIR" && docker compose up -d --force-recreate webserver)
    if container_id="$(docker ps -qf name=webserver | head -1)"; then
      echo "==> CF_* im Container: $(docker exec "$container_id" env | grep -c '^CF_' || true) Variablen"
    fi
  else
    echo "==> Hinweis: $compose_file nicht gefunden — Paperless-Recreate übersprungen"
  fi
fi

echo "==> Fertig."
if [[ -f "$TARGET/post_consume.py" ]]; then
  echo "==> post_consume: $(grep -m1 '^POST_CONSUME_VERSION' "$TARGET/post_consume.py" || true)"
fi
