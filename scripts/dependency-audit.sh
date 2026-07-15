#!/usr/bin/env bash
# CVE-Check für corr.manager-Abhängigkeiten (pip-audit).
# Auf CT 121: /opt/paperless-scripts/venv oder Repo-venv.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REQ="${ROOT}/requirements-corr-manager.txt"
VENV="${1:-/opt/paperless-scripts/venv}"

if [[ ! -f "$REQ" ]]; then
  echo "requirements nicht gefunden: $REQ" >&2
  exit 1
fi

PY="${VENV}/bin/python3"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

echo "=== pip-audit (${REQ}) ==="
"$PY" -m pip install -q pip-audit
"$PY" -m pip-audit -r "$REQ"
