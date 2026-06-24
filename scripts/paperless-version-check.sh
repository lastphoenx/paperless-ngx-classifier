#!/usr/bin/env bash
# Paperless-Version und Docker-Image auf CT 121 prüfen.
# Aufruf: ./scripts/paperless-version-check.sh
set -euo pipefail

COMPOSE_DIR="${PAPERLESS_COMPOSE_DIR:-/opt/paperless}"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
TARGET_VERSION="${PAPERLESS_TARGET_VERSION:-2.20.15}"

echo "=== Paperless Version Check ==="
echo ""

if [[ -f "$COMPOSE_FILE" ]]; then
  echo "docker-compose.yml ($COMPOSE_FILE):"
  grep -E '^\s*image:.*paperless-ngx' "$COMPOSE_FILE" || echo "  (kein paperless-ngx image gefunden)"
else
  echo "WARN: $COMPOSE_FILE nicht gefunden"
fi
echo ""

cid="$(docker ps -qf 'name=webserver' 2>/dev/null | head -1 || true)"
if [[ -z "$cid" ]]; then
  echo "WARN: kein laufender webserver-Container"
  exit 1
fi

image="$(docker inspect "$cid" --format '{{.Config.Image}}')"
echo "Laufendes Image:  $image"
echo "Ziel-Pin:         ghcr.io/paperless-ngx/paperless-ngx:$TARGET_VERSION"
echo ""

# Version aus Container (mehrere Fallbacks)
version=""
for cmd in \
  'cat /usr/src/paperless/src/paperless/version.py 2>/dev/null' \
  'python3 -c "import paperless; print(getattr(paperless, \"__version__\", \"\"))" 2>/dev/null' \
  'pip show paperless-ngx 2>/dev/null | grep -i ^Version'; do
  version="$(docker exec "$cid" bash -lc "$cmd" 2>/dev/null | tr -d '\r' | head -1 || true)"
  [[ -n "$version" ]] && break
done

if [[ -n "$version" ]]; then
  echo "App-Version im Container: $version"
else
  echo "App-Version: (nicht ermittelbar — API-Fallback unten)"
fi

token=""
if [[ -f "$COMPOSE_DIR/.env" ]]; then
  token="$(grep -m1 '^PAPERLESS_TOKEN=' "$COMPOSE_DIR/.env" | cut -d= -f2- || true)"
fi
if [[ -n "$token" ]]; then
  api_version="$(curl -sf --connect-timeout 5 --max-time 10 \
    -H "Authorization: Token $token" \
  http://127.0.0.1:8000/api/ 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('version', '?'))
except Exception:
    print('?')
" 2>/dev/null || echo "?")"
  echo "API /api/ version:      $api_version"
fi

echo ""
if [[ "$image" == *":latest" ]]; then
  echo "⚠ RISIKO: Image ist :latest — bei pull/up kann v3 gezogen werden."
  echo "  → Pin setzen: docs/UPGRADE_V3.md Phase 0"
elif [[ "$image" == *":$TARGET_VERSION" ]]; then
  echo "✓ Image ist auf $TARGET_VERSION gepinnt."
else
  echo "ℹ Image weicht vom Ziel-Pin $TARGET_VERSION ab: $image"
fi
