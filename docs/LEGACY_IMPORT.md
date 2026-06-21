# Legacy-Altbestand importieren

NAS-Ordner schrittweise in Paperless übernehmen — **nur Indexierung**, ohne OCR/Vision/LLM-Pipeline.

## Voraussetzungen (einmalig)

### 1. Tag in Paperless anlegen

Admin → Tags → **legacy** erstellen → Zuweisungsregel: **Keine Zuweisung**.

### 2. NAS auf dem Paperless-Host erreichbar machen

| Mount (Beispiel CT 121) | Quelle | Inhalt |
|-------------------------|--------|--------|
| `/mnt/paperless-media` | `nas-host:/srv/nas/Paperless/media` | Bereits importierte Paperless-PDFs |
| `/mnt/paperless-data/consume` | lokales Volume | Ziel für Legacy-Kopien |
| `/mnt/nas-legacy` | `nas-host:/mnt/ssd1` | Legacy-Quell-PDFs |

#### Wichtig: mergerfs auf pi-nas

`/srv/nas` ist auf pi-nas oft **mergerfs** (FUSE). NFS-Export von `/srv/nas` zeigt Unterordner auf Clients **unvollständig** (z. B. kein `Eltern/Finanzen`), obwohl lokal alles sichtbar ist.

**Lösung:** Auf pi-nas **`/mnt/ssd1`** exportieren (ext4, echte Platte), nicht `/srv/nas`.

**pi-nas** `/etc/exports` (IP des Paperless-Hosts anpassen):

```bash
/mnt/ssd1  PAPERLESS_HOST_IP(ro,sync,no_subtree_check,no_root_squash)
```

```bash
exportfs -ra
```

`no_root_squash` wegen Gruppe `parents` (`drwxrws---`) auf `Eltern/Finanzen`.

**Paperless-Host** `/etc/fstab`:

```fstab
nas-host.example:/mnt/ssd1  /mnt/nas-legacy  nfs4  rw,nfsvers=4.2,soft,timeo=600,retrans=2,_netdev  0  0
```

```bash
mkdir -p /mnt/nas-legacy
systemctl daemon-reload
mount -v /mnt/nas-legacy
ls /mnt/nas-legacy/Eltern/Finanzen | head
```

### 3. `.env` (produktiv manuell)

Siehe `.env.example` — mindestens:

```bash
LEGACY_CONSUME_MARKERS=/legacy/
LEGACY_TAG=legacy
PAPERLESS_CONSUMER_RECURSIVE=true
PAPERLESS_OCR_MODE=skip
PAPERLESS_TASK_WORKERS=1
```

Prüfen im Container:

```bash
docker exec $(docker ps -qf name=webserver | head -1) env | grep LEGACY
```

Nach Änderung:

```bash
cd /opt/paperless && docker compose up -d --force-recreate webserver
```

### 4. Code deployen

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/deploy-to-ct121.sh
```

Kopiert u. a. `pre_consume.sh`, `post_consume.py`, `legacy-import-batch.sh` nach `/opt/paperless-scripts/`.

## Ablauf

```
/mnt/nas-legacy/...  →  consume/legacy/<batch>/  (+ .pdf.json)
                              → pre_consume skip
                              → Paperless index (OCR skip)
                              → post_consume nur Tag legacy
```

**Niemals** NAS-Originale direkt als `consume/` mounten — Paperless **löscht** verarbeitete Dateien dort.

## Erster Test: Moni/2016 (kleiner Ordner)

Kandidat für einen ersten Lauf (ca. 10 Jahre alt — ggf. später auf NAS löschen, in Paperless bleibt es).

```bash
# Wie viele PDFs?
find /mnt/nas-legacy/Eltern/Finanzen/Vorsorge/Moni/2016 -name '*.pdf' | wc -l

# Dry-run
/opt/paperless-scripts/legacy-import-batch.sh \
  /mnt/nas-legacy/Eltern/Finanzen/Vorsorge/Moni/2016 \
  moni-2016-test \
  --dry-run

# Echter Lauf (ganzer Ordner — ohne --limit)
/opt/paperless-scripts/legacy-import-batch.sh \
  /mnt/nas-legacy/Eltern/Finanzen/Vorsorge/Moni/2016 \
  moni-2016-test
```

### Erfolg prüfen

1. `consume/legacy/moni-2016-test/` leert sich
2. Paperless: Filter `Tag: legacy` und `Tag: legacy-moni-2016-test`
3. Logs:
   ```bash
   docker compose -f /opt/paperless/docker-compose.yml logs webserver 2>&1 | \
     grep -E 'pre_consume.*Legacy|Legacy-Import' | tail -20
   ```
4. Volltextsuche in einem bekannten Dokument

## Weitere Testläufe

```bash
/opt/paperless-scripts/legacy-import-batch.sh \
  /mnt/nas-legacy/Eltern/Finanzen \
  eltern-finanzen \
  --limit 5 \
  --dry-run
```

## Produktiv-Import

Pro Batch warten bis `consume/legacy/<batch>/` leer ist:

```bash
/opt/paperless-scripts/legacy-import-batch.sh \
  /mnt/nas-legacy/Eltern/Finanzen \
  eltern-finanzen
```

`rsync --ignore-existing`: erneuter Lauf kopiert keine Duplikate.

## Sidecar-Format

`rechnung.pdf` → `rechnung.pdf.json`:

```json
{"tags": ["legacy", "legacy-moni-2016-test"]}
```

## Fehlerbehebung

| Symptom | Lösung |
|---------|--------|
| Pipeline läuft trotzdem (Vision, `pending_review`) | **Bug bis Pipe 12.29:** `post_consume` sieht nur `originals/`-Pfad, nicht `consume/legacy/`. Fix: **12.30+** deployen (Marker + `DOCUMENT_TAGS`). Fehlimporte löschen, Consume leeren, neu importieren. |
| `Eltern/Finanzen` auf Client leer, lokal auf NAS ok | mergerfs — `/mnt/ssd1` exportieren |
| `LEGACY` leer in Container | `.env` + `force-recreate webserver` |
| Permission denied | NFS `no_root_squash` oder ACL Gruppe `parents` |

### Fehlimport rückgängig (Pipeline lief versehentlich)

```bash
# 1) Deploy Fix (Pipe ≥ 12.30)
cd /opt/paperless-ngx-classifier && git pull && ./scripts/deploy-to-ct121.sh

# 2) Reste im Consume
rm -rf /mnt/paperless-data/consume/legacy/moni-2016-test/*

# 3) In Paperless UI: 8 Test-Dokumente löschen (Filter legacy-moni-2016-test oder Datum)

# 4) Neu importieren
/opt/paperless-scripts/legacy-import-batch.sh \
  /mnt/nas-legacy/Eltern/Finanzen/Vorsorge/Moni/2016 \
  moni-2016-test
```

Logs müssen zeigen: `Legacy-Import — Pipeline übersprungen` — **ohne** Vision/Ollama danach.

Neue Scans in `consume/` (ohne `legacy/`) → volle Pipeline unverändert.
