# Paperless-NGX v3 — Upgrade-Plan

Upgrade-Pfad für **CT 121** mit **paperless-ngx-classifier**, Legacy-Migration und paper.manager.

**Stand Produktion (Juni 2026):**

| Check | Status |
|-------|--------|
| App-Version | **2.20.15** |
| `docker-compose.yml` | gepinnt auf `2.20.15` |
| Laufender Container-Tag | noch `:latest` (gleicher Layer — harmlos bis `pull`) |
| v3 stable | **noch nicht** — Beta `3.0.0-beta.rc1` |
| Legacy-Import | **läuft noch** — vor v3 abschliessen |

**Empfehlung:** Phase 0 erledigt. Legacy fertig migrieren. v3 erst bei **stable 3.0.0** auf Testinstanz, dann Produktion.

Offizieller Upstream-Guide: [migration-v3.md](https://github.com/paperless-ngx/paperless-ngx/blob/dev/docs/migration-v3.md) (nach Release ggf. `main`-Branch prüfen).

---

## Übersicht — drei Phasen

| Phase | Inhalt | Status |
|-------|--------|--------|
| **0** | 2.20.15, Image pinnen, Version prüfen | ✓ erledigt |
| **1** | Legacy-Altbestand fertig migrieren | **läuft** |
| **2** | v3 stable: vorbereiten, testen, umschalten | wenn 3.0.0 stable |

---

## Phase 0 — Version prüfen und pinnen (erledigt)

### Version prüfen

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/paperless-version-check.sh
```

Manuell (API liefert Version in Headern, nicht als JSON unter `/api/`):

```bash
TOKEN=$(grep -m1 '^PAPERLESS_TOKEN=' /opt/paperless/.env | cut -d= -f2-)
curl -sI -H "Authorization: Token $TOKEN" \
  "http://127.0.0.1:8000/api/documents/?page_size=1" \
  | grep -iE '^(HTTP|x-api-version|x-version)'

docker exec "$(docker ps -qf name=webserver | head -1)" \
  python3 -c "exec(open('/usr/src/paperless/src/paperless/version.py').read()); print('.'.join(map(str, __version__)))"
```

### Pin in `/opt/paperless/docker-compose.yml`

```yaml
image: ghcr.io/paperless-ngx/paperless-ngx:2.20.15
```

**Niemals** `:latest` auf Produktion — bei `docker compose pull` kann sonst v3 gezogen werden.

### Optional: Container-Tag angleichen (ohne Neustart)

Wenn compose gepinnt ist, der laufende Container aber noch `:latest` heisst:

```bash
docker tag ghcr.io/paperless-ngx/paperless-ngx:latest \
           ghcr.io/paperless-ngx/paperless-ngx:2.20.15
```

Oder mit Recreate (lädt `.env` neu):

```bash
cd /opt/paperless
docker compose pull webserver
docker compose up -d --force-recreate webserver
./scripts/paperless-version-check.sh   # aus Repo
```

| Aktion | Sicher? |
|--------|---------|
| Pin in compose, kein `pull` | ✓ |
| `pull` **nach** Pin | ✓ — nur 2.20.15 |
| `:latest` + `pull` | ✗ — Risiko v3 |

---

## Phase 1 — Legacy-Migration (vor v3)

### Warum vor v3?

- v3: Duplikate standardmäßig **erlaubt** (`DELETE_DUPLICATES` explizit nötig)
- Consumer-, OCR- und Task-API ändern sich
- Weniger Variablen = einfachere Fehlersuche

Details: [LEGACY_MIGRATION_PLAN.md](./LEGACY_MIGRATION_PLAN.md)

```bash
/opt/paperless-scripts/legacy-tasks-summary.sh
/opt/paperless-scripts/legacy-nas-sha256.sh missing

tmux new -s legacy
/opt/paperless-scripts/legacy-nas-sha256.sh import-loop --batch queue --chunk 20
```

### Abschluss Phase 1

- [ ] `legacy-nas-sha256.sh missing` leer (oder nur bewusst übersprungen)
- [ ] Kein `legacy-migrate-resume` / `legacy-migrate-all` aktiv
- [ ] `consume/legacy` leer
- [ ] Vollbackup

---

## Phase 2 — Upgrade auf Paperless-NGX 3.0.0 stable

**Voraussetzungen:** Phase 0 + 1 abgeschlossen, Release **3.0.0 stable** (kein Beta),
Classifier-Repo mit v3-Anpassungen (siehe 2.3), Wartungsfenster eingeplant.

### 2.1 Checkliste vor dem Upgrade

- [ ] Vollbackup (Postgres, `data`, `media`, `.env`, `training/`)
- [ ] `./scripts/paperless-version-check.sh` → App **2.20.15**, compose gepinnt
- [ ] Kein laufender Legacy-Import
- [ ] Release Notes 3.0.0 gelesen
- [ ] Testinstanz (optional aber empfohlen) mit Kopie erfolgreich upgraded

### 2.2 Vollbackup

```bash
# Beispiel — euer paperless-backup.sh oder manuell:
# Postgres-Dump, /mnt/paperless-data/data, /mnt/paperless-media, /opt/paperless/.env
```

### 2.3 Classifier-Repo aktualisieren

Vor dem Image-Wechsel (oder im selben Wartungsfenster) Repo mit v3-fähigem Stand deployen:

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/deploy-to-ct121.sh
```

**Geplante Code-Anpassungen** (im Repo umsetzen, sobald v3 stable naht):

| Datei | Änderung |
|-------|----------|
| `post_consume.py` | `Accept: application/json; version=9` in `_headers()` |
| `correspondent_manager_app.py` | Gleicher Accept-Header |
| `scripts/legacy-tasks-summary.sh` | `task_type` statt `task_name`, Pagination |
| `scripts/legacy-duplicate-audit.sh` | `task_type`, v3-Duplikat-Meldungen |
| `post_consume.py` | `paperless_get_notes()`: Liste oder `{results:[]}` |

**Bereits v3-tauglich:** Pre/Post-Consume per Env; Swiss-QR (`pre_consume_qr.py`);
Custom Fields per ID; keine Positional-Args in Hooks.

### 2.4 `.env` für v3 anpassen (`/opt/paperless/.env`)

| v2 (aktuell CT 121) | v3 |
|---------------------|-----|
| `PAPERLESS_OCR_MODE=skip` | `PAPERLESS_OCR_MODE=auto` |
| `PAPERLESS_OCR_MODE=redo` | unverändert |
| `PAPERLESS_OCR_SKIP_ARCHIVE_FILE=*` | **entfernen** |
| — | `PAPERLESS_ARCHIVE_FILE_GENERATION=auto` (Default) |
| `PAPERLESS_CONSUMER_POLLING=10` | `PAPERLESS_CONSUMER_POLLING_INTERVAL=10` |
| `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` | **behalten** wenn Duplikate aus consume entfernt werden sollen |
| — | `PAPERLESS_SECRET_KEY` muss gesetzt sein |
| `CONSUMER_BARCODE_SCANNER` (falls gesetzt) | entfernen (nur zxing-cpp) |

Mapping `skip` → `auto`: Pre-Consume (`ocrmypdf`) legt Textschicht an → Paperless überspringt OCR wie bisher.

### 2.5 `docker-compose.yml` für v3

```yaml
  webserver:
    image: ghcr.io/paperless-ngx/paperless-ngx:3.0.0   # stable-Tag, nie :latest
    environment:
      PAPERLESS_REDIS: redis://broker:6379
      PAPERLESS_DBENGINE: postgresql    # ab v3 Pflicht
      PAPERLESS_DBHOST: db
      PAPERLESS_DBPASS: "…"
```

Auch in Repo-`docker-compose.yml` anpassen und committen.

### 2.6 Upgrade ausführen (Produktion)

```bash
cd /opt/paperless

# 1. Backup verifiziert?
# 2. .env + compose wie oben angepasst

docker compose pull webserver
docker compose up -d --force-recreate webserver

# Logs beobachten (Tantivy-Index-Rebuild, Migrationen)
docker compose logs -f webserver
```

### 2.7 Direkt nach dem ersten Start

Erwartetes Verhalten:

- **Tantivy** baut Suchindex neu (Zeit + CPU)
- **Task-Historie** leer
- Sessions ungültig wenn `SECRET_KEY` rotiert
- API-Default ohne Accept-Header: **v10**

```bash
cd /opt/paperless-ngx-classifier
./scripts/paperless-version-check.sh   # Ziel-Pin in Skript ggf. auf 3.0.0 anpassen

TOKEN=$(grep -m1 '^PAPERLESS_TOKEN=' /opt/paperless/.env | cut -d= -f2-)
curl -sI -H "Authorization: Token $TOKEN" \
  "http://127.0.0.1:8000/api/documents/?page_size=1" \
  | grep -iE 'x-version|x-api-version'
```

### 2.8 Funktionstest (Classifier)

- [ ] PDF in `consume/` → pre_consume → post_consume → Tags, CF, Storage Path
- [ ] paper.manager: Login (Authentik), Doc-Review, Proxy preview/thumb
- [ ] Pipeline-Notiz (`🤖 pipe v…`) wird geschrieben/ersetzt
- [ ] Suche: Notizen mit `notes.note:…` (Tantivy)
- [ ] Bei Login 403 hinter nginx: `PAPERLESS_TRUSTED_PROXIES` / `PAPERLESS_ALLAUTH_TRUSTED_CLIENT_IP_HEADER`

### 2.9 Rollback

v3-DB-Migration ist nicht trivial rückgängig zu machen.

```bash
# Image zurück
image: ghcr.io/paperless-ngx/paperless-ngx:2.20.15
# + Postgres/data/media aus Backup restore
```

**Backup vor Upgrade ist Pflicht.**

---

## Breaking Changes (Classifier-relevant)

1. Upgrade nur von **2.20.15**
2. `PAPERLESS_SECRET_KEY` Pflicht
3. `PAPERLESS_DBENGINE=postgresql` Pflicht
4. OCR/Archiv entkoppelt (`skip` entfällt → `auto`)
5. Consumer: `POLLING_INTERVAL`, `STABILITY_DELAY`, Regex-Ignore
6. Duplikate standardmäßig erlaubt
7. API v9/v10; Default v10
8. Tasks: paginiert, `task_type`
9. Whoosh → Tantivy (Auto-Rebuild)
10. Dokument-Versionen (API: `content` = letzte Version)
11. Verschlüsselung entfernt (bei uns n/a)
12. Paperless-Barcode: nur zxing-cpp (`pre_consume_qr.py` unberührt)

---

## Warum **nicht** jetzt (vor stable 3.0.0)?

| Aspekt | Stand |
|--------|-------|
| v3 stable | ✗ nur Beta |
| Legacy | ✗ noch offen |
| Classifier v3-Code | noch nicht im Repo |
| Risiko | Hoch bei Beta + laufender Migration |

---

## Nächste Schritte

```bash
# Jetzt: Legacy
/opt/paperless-scripts/legacy-nas-sha256.sh missing
# import-loop …

# Bei stable 3.0.0: diese Datei Phase 2 + INSTALL.md Abschnitt «Upgrade v3»
```

**Siehe auch:** [INSTALL.md](../INSTALL.md) (Erstinstallation + Upgrade-Verweis), [.env.example](../.env.example) (v3-Kommentarblock).
