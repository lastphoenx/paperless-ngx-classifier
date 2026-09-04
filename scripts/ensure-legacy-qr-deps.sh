#!/usr/bin/env bash
# Legacy QR-Split: Abhängigkeiten auf CT121 — Host UND Paperless-Container.
#
# Host:  /opt/paperless-scripts/venv          → corr-manager, legacy tools (Host-Python)
# Docker: /opt/paperless-scripts/venv-docker → pre_consume_qr (Container-Python)
#
# Das Host-venv ist im Container NICHT lauffähig (anderer Python-Pfad) — deshalb venv-docker.
set -euo pipefail

TARGET="${PAPERLESS_SCRIPTS_DIR:-/opt/paperless-scripts}"
VENV_HOST="$TARGET/venv"
VENV_DOCKER="$TARGET/venv-docker"
COMPOSE_DIR="${PAPERLESS_COMPOSE_DIR:-/opt/paperless}"
COMPOSE_FILE="${PAPERLESS_COMPOSE_FILE:-$COMPOSE_DIR/docker-compose.yml}"
CONTAINER="${PAPERLESS_CONTAINER:-webserver}"

# Debian Bookworm: libzbar0 — Trixie (Paperless-Container): libzbar0t64
zbar_apt_pkg() {
  if apt-cache show libzbar0t64 &>/dev/null; then
    echo libzbar0t64
  else
    echo libzbar0
  fi
}

# Import allein reicht nicht — decode() lädt libzbar.so; ohne ldconfig oft „shared library not found“.
PYZBAR_DECODE_TEST='from pyzbar.pyzbar import decode; from PIL import Image; assert decode(Image.new("L", (32, 32))) == []; print("zbar decode ok")'

host_apt() {
  local zbar_pkg
  zbar_pkg="$(zbar_apt_pkg)"
  echo "==> Host apt: poppler-utils ${zbar_pkg} zbar-tools ghostscript"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y poppler-utils "$zbar_pkg" zbar-tools ghostscript
  ldconfig
}

host_venv() {
  if [[ ! -d "$VENV_HOST" ]]; then
    echo "==> Host venv anlegen: $VENV_HOST"
    python3 -m venv "$VENV_HOST"
  fi
  echo "==> Host pip (corr-manager requirements)"
  "$VENV_HOST/bin/pip" install --upgrade pip
  if [[ -f "$(dirname "$0")/../requirements-corr-manager.txt" ]]; then
    "$VENV_HOST/bin/pip" install -r "$(dirname "$0")/../requirements-corr-manager.txt"
  else
    "$VENV_HOST/bin/pip" install pdf2image pyzbar pillow pypdf
  fi
  echo "==> Host Prüfung (import + decode)"
  "$VENV_HOST/bin/python3" -c "import pdf2image, pyzbar; print('host: pdf2image+pyzbar import ok')"
  "$VENV_HOST/bin/python3" -c "$PYZBAR_DECODE_TEST"
}

container_deps() {
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "WARN: $COMPOSE_FILE nicht gefunden — Container-Schritt übersprungen" >&2
    return 0
  fi

  echo "==> Container ($CONTAINER): apt + venv-docker"
  docker compose -f "$COMPOSE_FILE" exec -T -u root "$CONTAINER" bash -ec "
    set -euo pipefail
    if apt-cache show libzbar0t64 &>/dev/null; then ZBAR_PKG=libzbar0t64; else ZBAR_PKG=libzbar0; fi
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y poppler-utils \"\$ZBAR_PKG\" zbar-tools ghostscript
    ldconfig
    VENV_DOCKER=/opt/paperless-scripts/venv-docker
    if [[ ! -x \"\$VENV_DOCKER/bin/python3\" ]] || ! \"\$VENV_DOCKER/bin/python3\" -c 'import pyzbar' 2>/dev/null; then
      echo '==> Container venv-docker neu anlegen'
      rm -rf \"\$VENV_DOCKER\"
      python3 -m venv \"\$VENV_DOCKER\"
    fi
    \"\$VENV_DOCKER/bin/pip\" install --upgrade pip
    \"\$VENV_DOCKER/bin/pip\" install pdf2image pyzbar pillow pypdf
    \"\$VENV_DOCKER/bin/python3\" -c \"import pdf2image, pyzbar; print('container: pdf2image+pyzbar import ok')\"
    \"\$VENV_DOCKER/bin/python3\" -c '${PYZBAR_DECODE_TEST}'
    command -v pdftoppm
    command -v zbarimg
  "
}

host_apt
host_venv
container_deps

echo "==> Manuell prüfen:"
echo "    $VENV_HOST/bin/python3 -c \"from pyzbar.pyzbar import decode; ...\""
echo "    docker compose -f $COMPOSE_FILE exec $CONTAINER $VENV_DOCKER/bin/python3 -c \"from pyzbar.pyzbar import decode; ...\""
