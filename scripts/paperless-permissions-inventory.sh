#!/usr/bin/env bash
# Phase-0-Inventar: Paperless-Benutzer, Gruppen, Speicherpfade, Beispiel-Dokumentrechte.
# Auf CT 121 (oder Host mit Paperless-API) ausführen, Ausgabe hierher kopieren.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/paperless/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${PAPERLESS_TOKEN:?PAPERLESS_TOKEN fehlt — ENV_FILE setzen oder exportieren}"
BASE="${PAPERLESS_URL:-http://127.0.0.1:8000}"
ACCEPT="${PAPERLESS_API_ACCEPT:-application/json; version=9}"
HDR=( -H "Authorization: Bearer ${PAPERLESS_TOKEN}" -H "Accept: ${ACCEPT}" )

echo "========== Phase 0 — Paperless Permissions Inventar =========="
echo "Datum: $(date -Iseconds)"
echo "API:   ${BASE}"
echo ""

section() { echo ""; echo "---------- $* ----------"; }

section "1) Gruppen (id, name)"
curl -sf "${BASE}/api/groups/?page_size=100" "${HDR[@]}" \
  | jq -r '.results[] | "  id=\(.id)  name=\(.name)"' \
  | sort -t= -k2

section "2) Benutzer (id, username, superuser, group_ids)"
curl -sf "${BASE}/api/users/?page_size=100" "${HDR[@]}" \
  | jq -r '.results[] | "  id=\(.id)  user=\(.username)  super=\(.is_superuser)  groups=\(.groups | join(","))"'

section "3) Aktuelle .env Gruppen-Konfiguration (paper.manager / post_consume)"
if [[ -f "$ENV_FILE" ]]; then
  grep -E '^PAPERLESS_(VIEW|CHANGE)_(GROUP|GROUP_IDS)=' "$ENV_FILE" || true
else
  echo "  (kein $ENV_FILE)"
fi

section "4) Speicherpfade in Paperless (id, name, path, permissions)"
curl -sf "${BASE}/api/storage_paths/?page_size=200" "${HDR[@]}" \
  | jq -r '.results[] | "  id=\(.id)  path=\(.path)  name=\(.name)  owner=\(.owner)  view=\(.permissions.view_users // [] | join(","))/\(.permissions.view_groups // [] | join(","))  change=\(.permissions.change_users // [] | join(","))/\(.permissions.change_groups // [] | join(","))"'

section "5) Beispiel-Dokumente — Rechte nach Speicherpfad-Präfix"
curl -sf "${BASE}/api/documents/?page_size=20&ordering=-created" "${HDR[@]}" \
  | jq -r '
    .results[]
    | . as $d
    | ($d.storage_path | if . == null then "?" else tostring end) as $sp
    | "  doc=\($d.id)  storage_path_id=\($sp)  owner=\($d.owner)  view_users=\($d.permissions.view_users | join(","))  view_groups=\($d.permissions.view_groups | join(","))  change_users=\($d.permissions.change_users | join(","))  change_groups=\($d.permissions.change_groups | join(","))"'

section "6) NAS-Hauptordner unter originals/"
NAS_ROOT="${NAS_ORIGINALS:-/srv/nas/Paperless/media/documents/originals}"
if [[ -d "$NAS_ROOT" ]]; then
  ls -1 "$NAS_ROOT" | sed 's/^/  /'
else
  echo "  (Verzeichnis nicht gefunden: $NAS_ROOT)"
fi

section "7) Manifest-Hauptordner (paper.manager)"
MANIFEST="${MANIFEST_PATH:-/opt/paperless-scripts/manifest.json}"
if [[ -f "$MANIFEST" ]]; then
  jq -r '.ordner[].pfad | split("/")[0]' "$MANIFEST" | sort -u | sed 's/^/  /'
else
  echo "  (kein $MANIFEST)"
fi

echo ""
echo "========== Ende — bitte komplette Ausgabe zurückschicken =========="
