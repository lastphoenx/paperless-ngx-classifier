# Paperless-NGX v3 — Upgrade-Plan (CT 121)

Upgrade von **2.20.15** → **3.1.2** mit **paperless-ngx-classifier**, paper.manager und Legacy-Pipeline.

**Stand September 2026:**

| Check | Status |
|-------|--------|
| PBS-Backup / Snapshot CT 121 | ✓ erledigt |
| Legacy-Import abgeschlossen | ✓ erledigt |
| Classifier v3-Code im Repo | ✓ (pipe 12.77, be 2.63) |
| Ziel-Image | `ghcr.io/paperless-ngx/paperless-ngx:3.1.2` |

Offizieller Upstream-Guide: [migration-v3.md](https://github.com/paperless-ngx/paperless-ngx/blob/dev/docs/migration-v3.md)

Release Notes: [3.1.2](https://github.com/paperless-ngx/paperless-ngx/releases) (Security-Fix — empfohlen statt 3.0.0)

---

## Phasen-Übersicht

| Phase | Inhalt | Status |
|-------|--------|--------|
| **0** | PBS-Snapshot + Backup verifizieren | ✓ |
| **1** | Legacy-Altbestand fertig migrieren | ✓ |
| **2** | Classifier deployen (v3-API) | **jetzt** |
| **3** | `.env` + `docker-compose.yml` anpassen | **jetzt** |
| **4** | Image wechseln, Migrationen abwarten | **jetzt** |
| **5** | Smoke-Tests | nach Start |

---

## Phase 2 — Classifier deployen

**Auf CT 121** (Repo muss aktuellen Stand haben — `git pull`):

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/deploy-to-ct121.sh --no-docker
```

`--no-docker` hier bewusst: Paperless läuft noch auf v2. Nach Phase 3/4 ohne `--no-docker` oder manuell `docker compose up -d --force-recreate webserver`.

**Umgesetzte v3-Anpassungen im Repo:**

| Datei | Änderung |
|-------|----------|
| `post_consume.py` | `Accept: application/json; version=9`; `paperless_get_notes()` paginiert |
| `correspondent_manager_app.py` | Gleicher Accept-Header |
| `scripts/legacy-tasks-summary.sh` | `task_type` statt `task_name` |
| `scripts/legacy-duplicate-audit.sh` | `task_type`, Pagination |
| `scripts/paperless-version-check.sh` | Ziel-Pin `3.1.2` |

---

## Phase 3 — Konfiguration anpassen

### 3.1 Aktuellen Zustand sichern

```bash
cd /opt/paperless
docker compose exec webserver python manage.py version
cp docker-compose.yml docker-compose.yml.bak-v2
cp .env .env.bak-v2
```

### 3.2 `/opt/paperless/.env`

| v2 (CT 121) | v3 Aktion |
|-------------|-----------|
| `PAPERLESS_OCR_MODE=skip` | → `PAPERLESS_OCR_MODE=auto` |
| `PAPERLESS_OCR_SKIP_ARCHIVE_FILE=*` | **entfernen** |
| — | `PAPERLESS_ARCHIVE_FILE_GENERATION=always` (weil `skip` in v2 immer archivierte) |
| `PAPERLESS_CONSUMER_POLLING=10` | → `PAPERLESS_CONSUMER_POLLING_INTERVAL=10` |
| `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` | **behalten** (v3 erlaubt Duplikate sonst standardmäßig) |
| — | `PAPERLESS_DBENGINE=postgresql` |
| — | `PAPERLESS_AI_ENABLED=false` (optional explizit; UI-Werte haben Vorrang) |
| `CONSUMER_BARCODE_SCANNER` | entfernen falls gesetzt |

`skip` → `auto` ist bei uns korrekt: `pre_consume.sh` legt per ocrmypdf Text an → Paperless überspringt OCR wie bisher.

**Beispiel-Patch** (manuell prüfen, nicht blind ausführen):

```bash
cd /opt/paperless
sed -i 's/^PAPERLESS_OCR_MODE=skip/PAPERLESS_OCR_MODE=auto/' .env
sed -i '/^PAPERLESS_OCR_SKIP_ARCHIVE_FILE=/d' .env
sed -i '/^PAPERLESS_CONSUMER_POLLING=/d' .env
grep -q '^PAPERLESS_ARCHIVE_FILE_GENERATION=' .env || \
  echo 'PAPERLESS_ARCHIVE_FILE_GENERATION=always' >> .env
grep -q '^PAPERLESS_CONSUMER_POLLING_INTERVAL=' .env || \
  echo 'PAPERLESS_CONSUMER_POLLING_INTERVAL=10' >> .env
grep -q '^PAPERLESS_DBENGINE=' .env || \
  echo 'PAPERLESS_DBENGINE=postgresql' >> .env
grep -q '^PAPERLESS_AI_ENABLED=' .env || \
  echo 'PAPERLESS_AI_ENABLED=false' >> .env
grep -q '^PAPERLESS_CONSUMER_DELETE_DUPLICATES=' .env || \
  echo 'PAPERLESS_CONSUMER_DELETE_DUPLICATES=true' >> .env
```

### 3.3 `/opt/paperless/docker-compose.yml`

```yaml
  webserver:
    image: ghcr.io/paperless-ngx/paperless-ngx:3.1.2
    environment:
      PAPERLESS_REDIS: redis://broker:6379
      PAPERLESS_DBENGINE: postgresql
      PAPERLESS_DBHOST: db
      PAPERLESS_DBPASS: "…"
```

**Niemals** `:latest` auf Produktion.

---

## Phase 4 — Upgrade ausführen

Zeitplan: **2–3 Stunden** bei ~90k Dokumenten (DB-Migration + Tantivy-Rebuild).

```bash
cd /opt/paperless

docker compose pull webserver
docker compose up -d --force-recreate webserver

# Migrationen + Tantivy — NICHT abbrechen
docker compose logs -f webserver
```

Warten bis im Log steht:

```
Paperless-ngx document management system ready
```

**Tantivy:** Suchindex wird beim ersten Start **automatisch** neu gebaut. Manueller Reindex nur bei Problemen:

```bash
docker compose exec webserver python manage.py index --reindex
```

**Erwartetes Verhalten nach Start:**

- Task-Historie leer (v3-Redesign)
- Sessions ungültig nur wenn `PAPERLESS_SECRET_KEY` geändert wurde
- API-Default ohne Accept-Header: **v10**

### Version verifizieren

```bash
cd /opt/paperless-ngx-classifier
./scripts/paperless-version-check.sh

TOKEN=$(grep -m1 '^PAPERLESS_TOKEN=' /opt/paperless/.env | cut -d= -f2-)
curl -sI -H "Authorization: Token $TOKEN" \
  "http://127.0.0.1:8000/api/documents/?page_size=1" \
  | grep -iE 'x-version|x-api-version'
```

---

## Phase 5 — Smoke-Tests

- [ ] Paperless UI öffnet, Dokumente sichtbar
- [ ] Suche (Testbegriff; Notizen: `notes.note:…`)
- [ ] Frisches PDF in `consume/` → pre_consume → post_consume → Tags/CF/Pfad korrekt
- [ ] paper.manager unter `paperless.example.app/corr-manager/`
- [ ] `systemctl status correspondent-manager` — keine Errors
- [ ] `docker compose logs webserver` nach Testdokument — kein ERROR
- [ ] AI in Paperless UI deaktiviert (Settings → Application Configuration)

**Pipeline-Logs:**

```bash
docker compose logs -f webserver | grep -E "pre_consume|post_consume|ERROR"
```

**correspondent-manager:**

```bash
curl -s http://localhost:8100/api/config | python3 -m json.tool | head -20
journalctl -u correspondent-manager -n 50
```

---

## Rollback

v3-DB-Migration ist **nicht** per Image-Downgrade rückgängig.

```bash
# PBS-Snapshot CT 121 zurückspielen
# Danach optional:
cd /opt/paperless
cp docker-compose.yml.bak-v2 docker-compose.yml
cp .env.bak-v2 .env
docker compose up -d
```

---

## Breaking Changes (Classifier-relevant)

1. Upgrade nur von **2.20.15**
2. `PAPERLESS_SECRET_KEY` Pflicht (sollte bereits gesetzt sein)
3. `PAPERLESS_DBENGINE=postgresql` Pflicht
4. OCR/Archiv entkoppelt (`skip` → `auto` + `ARCHIVE_FILE_GENERATION`)
5. Consumer: `POLLING_INTERVAL`, Regex-Ignore statt fnmatch
6. Duplikate standardmäßig erlaubt → `DELETE_DUPLICATES=true` behalten
7. API v9/v10; Classifier nutzt `Accept: version=9`
8. Tasks: paginiert, `task_type`
9. Whoosh → Tantivy (Auto-Rebuild)
10. Paperless-Barcode: nur zxing-cpp (`pre_consume_qr.py` unberührt)

---

**Siehe auch:** [INSTALL.md](../INSTALL.md), [.env.example](../.env.example)
