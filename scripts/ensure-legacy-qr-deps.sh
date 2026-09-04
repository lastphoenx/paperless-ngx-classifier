#!/usr/bin/env bash
# Legacy QR-Split: Abhängigkeiten auf CT121 — Host UND Paperless-Container.
#
# Wichtig: pre_consume läuft im Docker-Container (webserver), nicht auf dem Host.
# apt/pip nur auf dem Host reicht nicht — Container braucht libzbar + venv-Pakete.
set -euo pipefail

TARGET="${PAPERLESS_SCRIPTS_DIR:-/opt/paperless-scripts}"
VENV="$TARGET/venv"
COMPOSE_DIR="${PAPERLESS_COMPOSE_DIR:-/opt/paperless}"
COMPOSE_FILE="${PAPERLESS_COMPOSE_FILE:-$COMPOSE_DIR/docker-compose.yml}"
CONTAINER="${PAPERLESS_CONTAINER:-webserver}"

host_apt() {
  echo "==> Host apt: poppler-utils libzbar0 zbar-tools ghostscript"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y poppler-utils libzbar0 zbar-tools ghostscript
}

host_venv() {
  if [[ ! -d "$VENV" ]]; then
    echo "==> Host venv anlegen: $VENV"
    python3 -m venv "$VENV"
  fi
  echo "==> Host pip (corr-manager requirements)"
  "$VENV/bin/pip" install --upgrade pip
  if [[ -f "$(dirname "$0")/../requirements-corr-manager.txt" ]]; then
    "$VENV/bin/pip" install -r "$(dirname "$0")/../requirements-corr-manager.txt"
  else
    "$VENV/bin/pip" install pdf2image pyzbar pillow pypdf
  fi
  echo "==> Host Prüfung"
  "$VENV/bin/python3" -c "import pdf2image, pyzbar; print('host: pdf2image+pyzbar ok')"
}

container_deps() {
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "WARN: $COMPOSE_FILE nicht gefunden — Container-Schritt übersprungen" >&2
    echo "      Setze PAPERLESS_COMPOSE_DIR oder lege docker-compose.yml unter /opt/paperless" >&2
    return 0
  fi

  echo "==> Container ($CONTAINER): apt + venv pip"
  docker compose -f "$COMPOSE_FILE" exec -T -u root "$CONTAINER" bash -ec "
    set -euo pipefail
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y poppler-utils libzbar0 zbar-tools ghostscript
    if [[ ! -x /opt/paperless-scripts/venv/bin/python3 ]]; then
      python3 -m venv /opt/paperless-scripts/venv
    fi
    /opt/paperless-scripts/venv/bin/pip install --upgrade pip
    /opt/paperless-scripts/venv/bin/pip install pdf2image pyzbar pillow pypdf
    /opt/paperless-scripts/venv/bin/python3 -c \"import pdf2image, pyzbar; print('container: pdf2image+pyzbar ok')\"
    command -v pdftoppm
    command -v zbarimg
  "
}

host_apt
host_venv
container_deps

echo "==> Test (Host): $VENV/bin/python3 $TARGET/legacy_qr_split_test.py /pfad/scan.pdf --verbose-pages"
echo "==> Test (Container): docker compose -f $COMPOSE_FILE exec $CONTAINER /opt/paperless-scripts/venv/bin/python3 -c \"import pyzbar; print('ok')\""
