# Legacy-Altbestand importieren

NAS-Ordner schrittweise in Paperless — **nur Indexierung**, ohne OCR/Vision/LLM-Pipeline.

Vollständiger Batch-Plan: [LEGACY_MIGRATION_PLAN.md](./LEGACY_MIGRATION_PLAN.md)

Paperless **2.20.15** (gepinnt). Legacy vor v3 abschliessen → [UPGRADE_V3.md](./UPGRADE_V3.md).

NFS-Architektur (CT 121): [ct121-nfs-fix.md](../../../doku/pve2/vm/121-paperless/Doku/docs/ct121-nfs-fix.md)

## Voraussetzungen (einmalig)

### 1. Tag in Paperless

Admin → Tags → **legacy** → Zuweisungsregel: **Keine Zuweisung**.

### 2. NFS auf CT 121

| Mount (Beispiel) | Export (pi-nas) | Zweck |
|------------------|-----------------|--------|
| `/mnt/paperless-media` | `:/mnt/ssd1/Paperless/media` rw | Paperless-Archiv |
| *eltern-mount* (aus fstab) | `:/mnt/ssd1/Eltern` ro | `…/Finanzen` Legacy-Import |

Vollständig: [NAS_NFS_AND_IMPORT.md](./NAS_NFS_AND_IMPORT.md)

```bash
grep 192.168.141.140 /etc/fstab
findmnt -t nfs4,nfs
touch /mnt/paperless-media/.write_test && rm /mnt/paperless-media/.write_test && echo OK
```

Details exports: `doku/.../ct121-nfs-fix.md`

### 3. `.env` (`/opt/paperless/.env`)

```bash
LEGACY_CONSUME_MARKERS=/legacy/
LEGACY_TAG=legacy
LEGACY_SET_BATCH_TAG=false
LEGACY_STORAGE_PATH_NAME=Legacy
LEGACY_STORAGE_PATH_TEMPLATE=legacy/{title}
PAPERLESS_CONSUMER_RECURSIVE=true
PAPERLESS_OCR_MODE=skip
PAPERLESS_TASK_WORKERS=1
# Optional manuell; legacy-migrate-all.sh setzt DELETE_DUPLICATES temporär auf true:
# PAPERLESS_CONSUMER_DELETE_DUPLICATES=true
```

Nach Änderung: `cd /opt/paperless-ngx-classifier && git pull && ./scripts/deploy-to-ct121.sh`

Pipe ≥ **12.32** erforderlich für Speicherpfad `legacy/{title}` und nur Tag `legacy`.

### 4. Code deployen

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/deploy-to-ct121.sh
grep POST_CONSUME_VERSION /opt/paperless-scripts/post_consume.py
```

## Ablauf

```
/mnt/nas-legacy/Eltern/Finanzen/...  →  rsync  →  consume/legacy/<batch>/
  → pre_consume skip  →  Paperless index (OCR skip)  →  post_consume: Tag legacy + Storage legacy/{title}
```

**Niemals** NAS-Originale direkt als `consume/` mounten — Paperless **löscht** verarbeitete Dateien dort.

Der Batch-Name (`blkb`, `steuern`, …) organisiert nur den Consume-Ordner — **kein** Paperless-Tag (wenn `LEGACY_SET_BATCH_TAG=false`).

## Smoke-Test (vor Bulk)

```bash
/opt/paperless-scripts/legacy-import-batch.sh \
  /mnt/nas-legacy/Eltern/Finanzen/BLKB blkb-smoke --limit 1
```

Warten bis `consume/legacy/blkb-smoke/` leer ist. Prüfen:

```bash
# pi-nas — nur ssd1
find /mnt/ssd1/Paperless/media/documents/originals/legacy -type f -mmin -30

# Logs
docker compose -f /opt/paperless/docker-compose.yml logs webserver --since 10m 2>&1 | \
  grep -iE 'Legacy-Import|Pipeline übersprungen'
```

UI: Tag **legacy** (ohne `legacy-blkb-smoke`), Speicherpfad `legacy/...`

Smoke-Dokument danach in der UI löschen.

## Erfolg prüfen (beliebiger Batch)

```bash
ls /mnt/paperless-data/consume/legacy/<batch>/    # leer = fertig
docker compose -f /opt/paperless/docker-compose.yml logs webserver --since 1h 2>&1 | \
  grep -c 'Legacy-Import.*Tags gesetzt'
```

Logs: `Legacy-Import — Pipeline übersprungen` — kein Vision/Ollama.

## Fehlerbehebung

| Symptom | Lösung |
|---------|--------|
| Pipeline läuft trotzdem | Pipe ≥ 12.30 deployen; Consume leeren; Docs löschen; neu importieren |
| `Eltern/Finanzen` leer auf CT121 | `nas-legacy` remounten; Export `:/srv/nas` ro auf pi-nas |
| Read-only / Webserver-Crash | Kein `/mnt/ssd1` ro-Gesamtexport; siehe ct121-nfs-fix.md |
| Stale file handle | `umount` + `mount -a` auf CT121 nach nfs-server restart |
| `none/none/` statt `legacy/` | `.env` LEGACY_STORAGE_* + Pipe 12.32 + recreate webserver |
| Migration hängt / Duplikate in consume | `legacy-migrate-all.sh --cleanup-consume`; Skip-Ordner nie unter `consume/` |
| `duplicate of #NNN` | Inhalt schon in Paperless — kein Re-Import nötig; optional `legacy`-Tag am bestehenden Doc |
| Wie viele Dubletten erwarten? | `legacy-nas-sha256.sh all` — SHA256-Inventar NAS + Abgleich Paperless-Checksums |
| Permission denied auf NAS | Export `no_root_squash` für `/srv/nas` ro |

Neue Scans in `consume/` (ohne `legacy/`) → volle Pipeline unverändert.
