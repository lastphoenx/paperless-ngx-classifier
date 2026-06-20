#!/usr/bin/env bash
# Deploy selected files from a local git clone to /opt/paperless-scripts on ct-121.
#
# One-time setup on ct-121:
#   git clone git@github.com:lastphoenx/paperless-ngx-classifier.git /opt/paperless-ngx-classifier
#
# Update (UI + backend + post_consume — Standard):
#   cd /opt/paperless-ngx-classifier && git pull && ./scripts/deploy-to-ct121.sh
#
# Zusätzlich pre_consume (selten):
#   ./scripts/deploy-to-ct121.sh --with-pre-consume

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${PAPERLESS_SCRIPTS_DIR:-/opt/paperless-scripts}"
WITH_PRE_CONSUME=0
RESTART=1

for arg in "$@"; do
  case "$arg" in
    --with-pre-consume|--with-pipeline) WITH_PRE_CONSUME=1 ;;
    --no-restart)    RESTART=0 ;;
  esac
done

FILES=(
  correspondent_manager_app.py
  paper_manager_ui.html
  post_consume.py
)

if [[ "$WITH_PRE_CONSUME" -eq 1 ]]; then
  FILES+=(
    pre_consume.sh
    pre_consume_qr.py
  )
fi

echo "==> Repo:   $REPO_DIR"
echo "==> Target: $TARGET"
for f in "${FILES[@]}"; do
  src="$REPO_DIR/$f"
  [[ -f "$src" ]] || { echo "FEHLER: $src fehlt" >&2; exit 1; }
  cp -v "$src" "$TARGET/$f"
done

if [[ "$RESTART" -eq 1 ]] && systemctl is-active --quiet correspondent-manager 2>/dev/null; then
  echo "==> Restart correspondent-manager"
  systemctl restart correspondent-manager
fi

echo "==> Fertig."
if [[ -f "$TARGET/post_consume.py" ]]; then
  echo "==> post_consume: $(grep -m1 '^POST_CONSUME_VERSION' "$TARGET/post_consume.py" || true)"
fi
