#!/bin/bash
# Remount fstab NFS (_netdev); restart Paperless webserver only if the mount was missing before.
# Called from dhcp-retry.service after dhcp-watch.sh, or from paperless-nfs-ensure.timer (CT 121).
set -u

MEDIA="${PAPERLESS_MEDIA_MOUNT:-/mnt/paperless-media}"
COMPOSE="${PAPERLESS_COMPOSE_DIR:-/opt/paperless}"

was_mounted=0
mountpoint -q "$MEDIA" && was_mounted=1

mount -a -O _netdev || true

if mountpoint -q "$MEDIA"; then
  if [ "$was_mounted" = 0 ]; then
    logger -t paperless-nfs "NFS neu aktiv ($MEDIA), starte webserver neu"
    cd "$COMPOSE" && docker compose restart webserver
  fi
else
  logger -t paperless-nfs "WARN: $MEDIA nicht gemountet nach mount -a"
fi
