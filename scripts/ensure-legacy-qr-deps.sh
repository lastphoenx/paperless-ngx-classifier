#!/usr/bin/env bash
# Legacy QR-Split: System- + Python-Abhängigkeiten (CT121 / paperless-scripts).
set -euo pipefail

TARGET="${PAPERLESS_SCRIPTS_DIR:-/opt/paperless-scripts}"
VENV="$TARGET/venv"

echo "==> apt: poppler-utils libzbar0 zbar-tools"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y poppler-utils libzbar0 zbar-tools

if [[ ! -d "$VENV" ]]; then
  echo "==> venv anlegen: $VENV"
  python3 -m venv "$VENV"
fi

echo "==> pip (corr-manager requirements)"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$(dirname "$0")/../requirements-corr-manager.txt" 2>/dev/null \
  || "$VENV/bin/pip" install pdf2image pyzbar pillow pypdf

echo "==> Prüfung"
"$VENV/bin/python3" -c "import pdf2image, pyzbar; print('pdf2image+pyzbar ok')"
command -v pdftoppm
command -v zbarimg
echo "==> Test mit: $VENV/bin/python3 $TARGET/legacy_qr_split_test.py /pfad/scan.pdf --verbose-pages"
