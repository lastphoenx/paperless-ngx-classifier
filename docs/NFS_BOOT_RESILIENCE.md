# NFS-Mount nach Reboot (Paperless-Host)

Paperless speichert PDFs und Thumbnails auf einem **NFS-Mount** (typisch `/mnt/paperless-media` → Docker `media`). Die Dokumentenliste kommt aus der **Datenbank** — die UI kann also „normal“ aussehen, während Vorschau und Download **404** liefern, weil der Mount fehlt.

## Symptome

| Beobachtung | Bedeutung |
|-------------|-----------|
| `findmnt /mnt/paperless-media` leer | Pfad ist kein NFS, nur Ordner auf der lokalen Root-Disk |
| `df` zeigt für `/mnt/paperless-media` das Root-FS (`/`) | Gleiches Problem |
| Browser: 404 auf `/api/documents/{id}/thumb/`, leerer Viewer | Media-Dateien nicht erreichbar |
| `curl` mit API-Token gegen `:8000` → 200 nach manuellem `mount` | Backend ok, Boot-Mount fehlte |

Häufig nach **Hypervisor-/Firewall-Reboot** oder wenn der NFS-Server später hochkommt als der Paperless-CT.

## Sofort-Fix (manuell)

```bash
cd /opt/paperless && docker compose stop webserver
mount -a -O _netdev
mountpoint -q /mnt/paperless-media && echo NFS_OK || echo NFS_FEHLER
cd /opt/paperless && docker compose up -d webserver
```

## Installation aus diesem Repo (CT 121)

Pfad zum Clone auf dem Host anpassen:

```bash
REPO=/opt/paperless-ngx-classifier
cd "$REPO" && git pull origin main

install -m 755 "$REPO/scripts/paperless-nfs-remount.sh" /usr/local/sbin/paperless-nfs-remount.sh
```

**Boot (einmalig, ~2 Minuten DHCP-Fenster):** Host muss bereits `/usr/local/sbin/dhcp-watch.sh` haben.

```bash
cp "$REPO/scripts/dhcp-retry.service.example" /etc/systemd/system/dhcp-retry.service
systemctl daemon-reload
systemctl enable --now dhcp-retry.service
```

**Spätes Netz/NAS (empfohlen, alle 5 Min):**

```bash
cp "$REPO/scripts/paperless-nfs-ensure.service.example" /etc/systemd/system/paperless-nfs-ensure.service
cp "$REPO/scripts/paperless-nfs-ensure.timer.example" /etc/systemd/system/paperless-nfs-ensure.timer
systemctl daemon-reload
systemctl enable --now paperless-nfs-ensure.timer
systemctl list-timers paperless-nfs-ensure.timer
```

### Was `paperless-nfs-remount.sh` macht

1. Merkt, ob `/mnt/paperless-media` **vorher** schon gemountet war.
2. `mount -a -O _netdev`
3. Mount **neu** gekommen → einmal `docker compose restart webserver` in `/opt/paperless`.
4. War NFS schon da → **kein** Restart (wichtig für den 5-Minuten-Timer).

### Test

```bash
/usr/local/sbin/paperless-nfs-remount.sh
journalctl -t paperless-nfs -n 5
grep was_mounted /usr/local/sbin/paperless-nfs-remount.sh   # Skript-Version mit idempotentem Restart
```

## Verifikation

```bash
findmnt /mnt/paperless-media
mountpoint -q /mnt/paperless-media && echo OK
```

API-Thumb (Token aus `/opt/paperless/.env`, Wert nicht loggen):

```bash
TOKEN="$(grep -m1 '^PAPERLESS_TOKEN=' /opt/paperless/.env | cut -d= -f2- | tr -d '"')"
DOC_ID=1
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  -H "Authorization: Token ${TOKEN}" \
  "http://127.0.0.1:8000/api/documents/${DOC_ID}/thumb/"
```

## Einige Docs 404, NFS wieder da (lokale Schicht)

Neue Dateien während NFS-Ausfall landen unter `/mnt/paperless-media` auf der **Root-Disk**; nach NFS-Mount sind sie versteckt, DB zeigt die Docs trotzdem.

Kurzablauf: Webserver stoppen → `umount` Media → `rsync -rlptDv --no-owner --no-group` lokal → NFS-Staging-Mount → `mount -a` → Webserver starten. Vollständige Schritte im privaten Ops-Repo `doku/pve2/vm/121-paperless/Doku/docs/ct121-nfs-fix.md` (Abschnitt „nur einige Docs Thumb 404“).

## Spätes Netzwerk

`dhcp-retry.service` (oneshot) deckt nur ~**2 Minuten** nach Boot ab. Der **Timer** (`paperless-nfs-ensure.timer`) holt NFS nach, wenn NAS/Firewall später kommt.

Betreiber-Homelab: fstab, Firewall, Merge-Runbook in `doku/.../ct121-nfs-fix.md`.
