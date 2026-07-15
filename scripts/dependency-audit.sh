#!/usr/bin/env bash
# CVE-Check für corr.manager-Abhängigkeiten (pip-audit).
# Nutzt immer ein venv — pip-audit ist kein System-Paket auf CT 121.
#
#   ./scripts/dependency-audit.sh
#   ./scripts/dependency-audit.sh /opt/paperless-scripts/venv
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REQ="${ROOT}/requirements-corr-manager.txt"
VENV="${1:-/opt/paperless-scripts/venv}"

if [[ ! -f "$REQ" ]]; then
  echo "FEHLER: requirements nicht gefunden: $REQ" >&2
  exit 1
fi

PY="${VENV}/bin/python3"
if [[ ! -x "$PY" ]]; then
  echo "FEHLER: venv nicht gefunden: $VENV" >&2
  echo "  Erwartet: ${VENV}/bin/python3" >&2
  echo "  Usage: $0 [/opt/paperless-scripts/venv]" >&2
  exit 1
fi

echo "=== pip-audit ==="
echo "  venv: $VENV"
echo "  req:  $REQ"
echo ""

"$PY" -m pip install -q --upgrade pip pip-audit

# Modul heißt pip_audit (Unterstrich) — «pip-audit» als CLI liegt nur in venv/bin/
if [[ -x "${VENV}/bin/pip-audit" ]]; then
  "${VENV}/bin/pip-audit" -r "$REQ"
else
  "$PY" -m pip_audit -r "$REQ"
fi
