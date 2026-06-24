#!/usr/bin/env bash
# Paperless-Version und Docker-Image auf CT 121 prüfen.
# Aufruf: ./scripts/paperless-version-check.sh
set -euo pipefail

COMPOSE_DIR="${PAPERLESS_COMPOSE_DIR:-/opt/paperless}"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
TARGET_VERSION="${PAPERLESS_TARGET_VERSION:-2.20.15}"
TARGET_IMAGE="ghcr.io/paperless-ngx/paperless-ngx:$TARGET_VERSION"

echo "=== Paperless Version Check ==="
echo ""

compose_image=""
if [[ -f "$COMPOSE_FILE" ]]; then
  echo "docker-compose.yml ($COMPOSE_FILE):"
  compose_image="$(grep -E '^\s*image:.*paperless-ngx' "$COMPOSE_FILE" | head -1 | awk '{print $2}' || true)"
  echo "  $compose_image"
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
echo "Ziel-Pin:         $TARGET_IMAGE"
echo ""

# App-Version aus version.py (__version__-Tuple)
app_version="$(docker exec "$cid" python3 -c "
from pathlib import Path
p = Path('/usr/src/paperless/src/paperless/version.py')
if not p.is_file():
    raise SystemExit(1)
ns = {}
exec(p.read_text(), ns)
v = ns.get('__version__')
if v:
    print('.'.join(map(str, v)))
elif ns.get('__full_version_str__'):
    print(ns['__full_version_str__'])
" 2>/dev/null || true)"

if [[ -n "$app_version" ]]; then
  echo "App-Version im Container: $app_version"
else
  echo "App-Version im Container: (nicht ermittelbar)"
fi

token=""
if [[ -f "$COMPOSE_DIR/.env" ]]; then
  token="$(grep -m1 '^PAPERLESS_TOKEN=' "$COMPOSE_DIR/.env" | cut -d= -f2- || true)"
fi

api_version=""
api_api_version=""
if [[ -n "$token" ]]; then
  headers="$(curl -sI --connect-timeout 5 --max-time 10 \
    -H "Authorization: Token $token" \
    "http://127.0.0.1:8000/api/documents/?page_size=1" 2>/dev/null || true)"
  api_version="$(printf '%s\n' "$headers" | grep -i '^x-version:' | head -1 | cut -d' ' -f2- | tr -d '\r' || true)"
  api_api_version="$(printf '%s\n' "$headers" | grep -i '^x-api-version:' | head -1 | cut -d' ' -f2- | tr -d '\r' || true)"
  echo "API x-version:          ${api_version:-?}"
  echo "API x-api-version:      ${api_api_version:-?}"
else
  echo "API:                    (kein PAPERLESS_TOKEN in $COMPOSE_DIR/.env)"
fi

echo ""
echo "--- Bewertung ---"

compose_ok=0
if [[ "$compose_image" == *":$TARGET_VERSION" ]]; then
  compose_ok=1
  echo "✓ compose.yml gepinnt auf $TARGET_VERSION"
elif [[ "$compose_image" == *":latest" ]]; then
  echo "⚠ compose.yml noch :latest — Pin setzen (docs/UPGRADE_V3.md Phase 0)"
else
  echo "ℹ compose.yml: $compose_image"
fi

if [[ "$image" == *":$TARGET_VERSION" ]]; then
  echo "✓ laufender Container nutzt Image-Tag $TARGET_VERSION"
elif [[ "$image" == *":latest" ]]; then
  if [[ "$compose_ok" -eq 1 ]]; then
    echo "ℹ Container noch :latest, compose aber gepinnt — ok bis zum nächsten pull/recreate."
    echo "  Optional ohne Neustart: docker tag $image $TARGET_IMAGE"
    echo "  Oder: cd $COMPOSE_DIR && docker compose up -d --force-recreate webserver"
  else
    echo "⚠ RISIKO: Container :latest — bei pull/up kann v3 gezogen werden."
  fi
else
  echo "ℹ laufendes Image: $image"
fi

if [[ -n "$app_version" && "$app_version" != "$TARGET_VERSION" ]]; then
  echo "⚠ App-Version $app_version weicht von Ziel-Pin $TARGET_VERSION ab"
elif [[ -n "$app_version" && "$app_version" == "$TARGET_VERSION" ]]; then
  echo "✓ App-Version entspricht Ziel-Pin $TARGET_VERSION"
fi
