# Legacy-Altbestand importieren

NAS-Ordner (verstreut auf pi-nas) schrittweise in Paperless übernehmen — **nur Indexierung**, ohne OCR/Vision/LLM-Pipeline.

## Voraussetzungen (einmalig)

### 1. Tag in Paperless anlegen

Admin → Tags → **legacy** erstellen → Zuweisungsregel: **Keine Zuweisung**.

### 2. NAS auf CT 121 erreichbar machen

**Aktueller Stand auf CT 121** (typisch):

| Mount | Quelle | Inhalt |
|-------|--------|--------|
| `/mnt/paperless-media` | `192.168.141.140:/srv/nas/Paperless/media` | Bereits importierte Paperless-PDFs |
| `/mnt/paperless-data/consume` | lokales LVM | Consume-Ordner (Ziel für Legacy-Kopien) |
| `/srv/nas` | — | **existiert nicht** auf CT 121 |

Legacy-PDFs liegen auf **pi-nas** (`192.168.141.140`) unter `/srv/nas/Eltern/…`, `/srv/nas/Thomas/…` usw. — **nicht** unter `Paperless/media`.

#### Option A (empfohlen): NFS-Readonly-Mount auf CT 121

**Auf pi-nas** (`/etc/exports`) — CT 121 hat i. d. R. `192.168.131.31`:

```bash
# Read-only für Legacy-Import
/srv/nas  192.168.131.31(ro,sync,no_subtree_check)
```

Dann auf pi-nas: `exportfs -ra`

**Auf CT 121** (`/etc/fstab`):

```fstab
192.168.141.140:/srv/nas  /mnt/nas-legacy  nfs4  rw,nfsvers=4.2,soft,timeo=600,retrans=2,_netdev  0  0
```

```bash
mkdir -p /mnt/nas-legacy
mount -a
# Prüfen:
ls /mnt/nas-legacy/Eltern/Finanzen | head
find /mnt/nas-legacy -name '*.pdf' | wc -l
```

> Export ist `ro` auf NFS-Server — Mount-Option `rw` auf dem Client ist ok; Schreibzugriff kommt vom Server.

#### Option B: rsync über SSH (ohne NFS-Export)

Wenn NFS nicht gewünscht — Staging auf CT 121 per `rsync` von pi-nas (User braucht **Lese-Recht** auf die Ordner):

```bash
# Beispiel — User/Host anpassen
rsync -a paperlessbackup@192.168.141.140:/srv/nas/Eltern/Finanzen/ \
  /mnt/nas-legacy/Eltern/Finanzen/
```

`paperlessbackup` hat auf pi-nas aktuell **kein** Leserecht auf `Eltern/Finanzen` → ACL/`exports` oder dedizierter User nötig (siehe unten).

#### Quellpfad für `legacy-import-batch.sh`

| Umgebung | Beispiel-Quellpfad |
|----------|-------------------|
| CT 121 mit NFS | `/mnt/nas-legacy/Eltern/Finanzen` |
| pi-nas direkt | `/srv/nas/Eltern/Finanzen` (nur wenn Script dort läuft) |

Umgebungsvariable (optional): `LEGACY_NAS_ROOT=/mnt/nas-legacy` — nur Dokumentation/Hilfstext; das Script erwartet den **vollen Quellpfad** als erstes Argument.

### 3. `.env` auf CT 121 (`/opt/paperless/.env`)

Produktiv manuell ergänzen:

```bash
LEGACY_CONSUME_MARKERS=/legacy/
LEGACY_TAG=legacy
PAPERLESS_CONSUMER_RECURSIVE=true
PAPERLESS_OCR_MODE=skip
PAPERLESS_TASK_WORKERS=1
```

`LEGACY_*` muss im **Container** ankommen (pre/post_consume laufen dort):

```bash
docker exec $(docker ps -qf name=webserver | head -1) env | grep LEGACY
```

Nach `.env`-Änderung:

```bash
cd /opt/paperless && docker compose up -d --force-recreate webserver
```

### 4. Code deployen

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/deploy-to-ct121.sh
```

Deploy kopiert u. a. `pre_consume.sh`, `post_consume.py`, `legacy-import-batch.sh` nach `/opt/paperless-scripts/`.

## Ablauf pro NAS-Ordner

```
/mnt/nas-legacy/...     rsync (Kopie)          Paperless consume
─────────────────  ──────────────────────►  consume/legacy/<batch>/
                                              + datei.pdf.json (Tags)
                                                    │
                                                    ▼
                                              pre_consume  → skip
                                              Paperless    → OCR skip, index
                                              post_consume → nur Tag legacy
```

**Wichtig:** Niemals NAS-Originale direkt als `consume/` mounten — Paperless **löscht** verarbeitete Dateien aus `consume/`.

## Testlauf (kleiner Ordner)

```bash
# Dry-run
/opt/paperless-scripts/legacy-import-batch.sh \
  /mnt/nas-legacy/Eltern/Finanzen \
  finanzen-test \
  --limit 5 \
  --dry-run

# Echter Lauf
/opt/paperless-scripts/legacy-import-batch.sh \
  /mnt/nas-legacy/Eltern/Finanzen \
  finanzen-test \
  --limit 5
```

### Erfolg prüfen

1. **Consume-Ordner leert sich** (`/mnt/paperless-data/consume/legacy/finanzen-test/`)
2. **Paperless UI:** Filter `Tag: legacy` und `Tag: legacy-finanzen-test`
3. **Logs:**
   ```bash
   docker compose -f /opt/paperless/docker-compose.yml logs webserver 2>&1 | \
     grep -E 'pre_consume.*Legacy|Legacy-Import' | tail -20
   ```
   Erwartung:
   - `[pre_consume] Legacy-Import — übersprungen`
   - `Legacy-Import — Pipeline übersprungen`
4. **Volltextsuche** nach einem bekannten Begriff aus einem Test-PDF

### Wenn etwas schiefgeht

| Symptom | Lösung |
|---------|--------|
| `Permission denied` auf pi-nas | NFS-Mount (Option A) oder ACL für Import-User; `/srv/nas` existiert nicht auf CT 121 ohne Mount |
| Pipeline läuft trotzdem (Vision in Logs) | `LEGACY_CONSUME_MARKERS` in `.env`, Container recreate |
| `LEGACY` leer in `docker exec env` | `.env` in `docker-compose` `env_file`, recreate |
| Kein Tag `legacy` | Tag in Paperless anlegen; Sidecar `.pdf.json` prüfen |
| Dateien bleiben in consume/ | `docker compose logs webserver` auf Fehler prüfen |

## Produktiv-Import

Pro Batch warten bis `consume/legacy/<batch>/` leer ist:

```bash
/opt/paperless-scripts/legacy-import-batch.sh \
  /mnt/nas-legacy/Eltern/Finanzen \
  eltern-finanzen
```

`rsync --ignore-existing` im Script: erneuter Lauf kopiert keine Duplikate.

## Sidecar-Format

Für `rechnung.pdf` wird `rechnung.pdf.json` erzeugt:

```json
{"tags": ["legacy", "legacy-finanzen-test"]}
```

Paperless wendet Tags beim Import an. `post_consume.py` setzt `legacy` zusätzlich als Fallback per API.

## pi-nas: Berechtigungen (Permission denied)

Wenn `ssh paperlessbackup@192.168.141.140 'ls /srv/nas/Eltern/Finanzen'` scheitert:

1. **NFS-Export** für CT 121 (Option A) — umgeht SSH-User-Rechte
2. Oder auf pi-nas ACL für Import-User, z. B.:
   ```bash
   # Beispiel — Pfade/User anpassen
   setfacl -R -m u:paperlessbackup:rx /srv/nas/Eltern /srv/nas/Thomas
   ```
3. Inventur auf pi-nas als root:
   ```bash
   find /srv/nas -name '*.pdf' | wc -l
   ```

Neue Scans direkt in `consume/` (ohne `legacy/`) laufen weiter mit voller Pipeline.
