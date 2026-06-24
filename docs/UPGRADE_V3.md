# Paperless-NGX v3 — Upgrade-Plan (Stand: 24.06.2026)

Dieses Dokument beschreibt den **sicheren Weg von 2.20.15 nach v3** für CT 121
mit **paperless-ngx-classifier**, Legacy-Migration und paper.manager.

**Empfehlung heute (24.06.2026):** v3 **nicht** auf Produktion fahren.
Stable ist weiterhin 2.x; v3 läuft als Pre-Release (`3.0.0-beta.rc1`).
Zuerst **2.20.15 pinnen**, **Legacy-Migration abschliessen**, dann v3 auf Testinstanz.

---

## Strategie in drei Phasen

| Phase | Was | Wann |
|-------|-----|------|
| **0** | Version prüfen, auf 2.20.15, Image pinnen | **Jetzt** |
| **1** | Legacy-Altbestand fertig migrieren | **Vor v3** |
| **2** | v3 vorbereiten / testen / umschalten | **Erst wenn v3 stable** |

**Bewusst nicht jetzt:** v3-only Env-Variablen oder Classifier-Code für v3 deployen.
Das Repo enthält nur den **Pin** und diese Doku — Produktion bleibt unverändert bis Phase 2.

---

## Phase 0 — Version prüfen und 2.20.15 pinnen

### 0.1 Version auf CT 121 prüfen

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/paperless-version-check.sh
```

Alternativ manuell:

```bash
grep 'paperless-ngx' /opt/paperless/docker-compose.yml
docker ps --format '{{.Image}}' --filter name=webserver
curl -s -H "Authorization: Token $TOKEN" http://127.0.0.1:8000/api/ | python3 -m json.tool
```

### 0.2 Falls noch nicht 2.20.15 — Security-Update einspielen

2.20.15 ist **Pflicht-Voraussetzung** für v3 (Security-Fix GHSA-8c6x-pfjq-9gr7).

```bash
cd /opt/paperless

# Backup (Pflicht vor jedem Image-Wechsel)
# → paperless-backup.sh oder DB + /mnt/paperless-media + /mnt/paperless-data

# docker-compose.yml anpassen (oder aus Repo kopieren):
#   image: ghcr.io/paperless-ngx/paperless-ngx:2.20.15

docker compose pull webserver
docker compose up -d --force-recreate webserver

./scripts/paperless-version-check.sh   # aus Repo, siehe 0.1
```

**Wichtig:** `docker compose pull` ohne Pin zieht ggf. schon v3 — deshalb **zuerst** pinnen, **dann** pull.

### 0.3 Image pinnen (dauerhaft)

Im Repo (`docker-compose.yml`):

```yaml
image: ghcr.io/paperless-ngx/paperless-ngx:2.20.15
```

Auf CT 121 dieselbe Zeile in `/opt/paperless/docker-compose.yml` setzen.

| Aktion | Sicher bei 2.20.15? |
|--------|---------------------|
| Image-Pin in compose | ✓ Ja |
| `docker compose up -d` (ohne pull) | ✓ Ja — nutzt vorhandenes Image |
| `docker compose pull` nach Pin | ✓ Ja — zieht nur 2.20.15 |
| `:latest` belassen | ✗ **Nein** — Risiko v3 bei pull/up |

### 0.4 Was in Phase 0 **nicht** geändert wird

- `/opt/paperless/.env` — OCR, Consumer, Duplikate bleiben wie sie sind
- Classifier-Skripte — kein v3-spezifischer Code
- `PAPERLESS_DBENGINE` — erst bei v3 nötig (2.20.15 inferiert Postgres aus `PAPERLESS_DBHOST`)

---

## Phase 1 — Legacy-Migration zuerst (empfohlen)

### Warum Legacy **vor** v3?

1. **Duplikat-Verhalten:** v3 lehnt Duplikate standardmäßig **nicht** mehr ab.
   Eure Legacy-Pipeline nutzt `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` und
   `legacy-duplicate-audit.sh` — auf 2.20.15 ist das Verhalten bekannt und dokumentiert.

2. **Consumer-Umbau in v3:** `CONSUMER_POLLING` → `CONSUMER_POLLING_INTERVAL`,
   Stability-Delay, Regex-Ignore — während eines Gross-Imports mehr Variablen.

3. **Task-API:** v3 paginiert Tasks, `task_name` → `task_type`.
   `legacy-tasks-summary.sh` muss für v3 angepasst werden — auf 2.20.15 läuft es heute.

4. **OCR-Mapping:** Legacy nutzt `PAPERLESS_OCR_MODE=skip` (v2-Semantik).
   In v3 wird das zu `auto` + ggf. `ARCHIVE_FILE_GENERATION` — ein zusätzlicher
   Umstellungs-Schritt mitten in der Migration.

5. **Weniger Moving Parts:** Ein Gross-Import + ein Major-Upgrade gleichzeitig
   erschwert Fehleranalyse.

6. **Tantivy-Rebuild:** v3 baut den Suchindex neu — irrelevant für Import,
   aber zusätzliche Last direkt nach Upgrade.

**Fazit:** Legacy auf 2.20.15 fertigstellen, dann v3 als separaten, geplanten Schritt.

### Legacy-Status und nächste Schritte

Siehe [LEGACY_MIGRATION_PLAN.md](./LEGACY_MIGRATION_PLAN.md).

```bash
/opt/paperless-scripts/legacy-tasks-summary.sh
/opt/paperless-scripts/legacy-nas-sha256.sh all
/opt/paperless-scripts/legacy-nas-sha256.sh missing   # Delta

tmux new -s legacy
/opt/paperless-scripts/legacy-nas-sha256.sh import-loop --batch queue --chunk 20
```

Offene NAS-Ordner (Stand Plan): Fano, Bestellungen, BLKB, Ameritrade, Erb_Bern,
Erbschaft_Gassacker, Steuern, Rechnungen, Vorsorge/…

### Abschluss-Kriterium Phase 1

- [ ] `legacy-nas-sha256.sh missing` leer oder nur bewusst übersprungene Dateien
- [ ] Kein aktiver `legacy-migrate-resume` / `legacy-migrate-all` im Hintergrund
- [ ] `legacy-tasks-summary.sh`: consume/legacy leer, Fehlgeschlagene erklärbar
- [ ] Vollbackup nach Abschluss

---

## Phase 2 — v3 vorbereiten, testen, umschalten

**Erst starten wenn:** Legacy fertig, 2.20.15 gepinnt, v3 **stable** (nicht Beta),
Testinstanz erfolgreich.

### 2.1 Vollbackup

- Postgres (`/mnt/paperless-data/postgres`)
- `/mnt/paperless-data/data`
- `/mnt/paperless-media`
- `/opt/paperless/.env`
- `/opt/paperless-scripts/training/`

### 2.2 Env-Änderungen für v3 (`/opt/paperless/.env`)

| v2 (aktuell) | v3 |
|--------------|-----|
| `PAPERLESS_OCR_MODE=skip` | `PAPERLESS_OCR_MODE=auto` (Default) |
| `PAPERLESS_OCR_MODE=skip_noarchive` | `auto` + `PAPERLESS_ARCHIVE_FILE_GENERATION=never` |
| `PAPERLESS_OCR_SKIP_ARCHIVE_FILE=*` | entfernen → `PAPERLESS_ARCHIVE_FILE_GENERATION` |
| `PAPERLESS_CONSUMER_POLLING=10` | `PAPERLESS_CONSUMER_POLLING_INTERVAL=10` |
| `PAPERLESS_CONSUMER_DELETE_DUPLICATES` (optional) | **explizit** setzen wenn altes Verhalten gewünscht |
| — | `PAPERLESS_DBENGINE=postgresql` (in compose oder .env) |
| `PAPERLESS_SECRET_KEY` | muss gesetzt sein (bei euch bereits in `.env.example`) |

In `docker-compose.yml` zusätzlich:

```yaml
environment:
  PAPERLESS_DBENGINE: postgresql
  PAPERLESS_DBHOST: db
```

### 2.3 Classifier-Anpassungen (bei v3-Switch deployen)

Noch **nicht** auf Produktion — Liste für Phase 2:

| Datei | Änderung |
|-------|----------|
| `post_consume.py` | `Accept: application/json; version=9` (oder 10) in `_headers()` |
| `correspondent_manager_app.py` | Gleicher Accept-Header in `PAPERLESS_HEADERS` |
| `scripts/legacy-tasks-summary.sh` | `task_name` → `task_type`, Pagination |
| `scripts/legacy-duplicate-audit.sh` | `task_type`, Duplikat-Meldungen v3 beachten |
| `post_consume.py` | `paperless_get_notes()`: Response als Liste oder `{results:[]}` |
| `.env.example`, `INSTALL.md`, `LEGACY_MIGRATION_PLAN.md` | v3-OCR-/Consumer-Doku |

**Nicht betroffen (bereits v3-tauglich):**

- `pre_consume.sh` / `post_consume.py` — nutzen Env-Variablen, keine `$1`…`$8`
- `pre_consume_qr.py` — eigenes pyzbar, unabhängig vom Paperless-Barcode-Backend
- Custom Fields per ID, `/api/`-Endpoints, bulk_edit

### 2.4 Image auf v3 (wenn stable)

```bash
cd /opt/paperless
# docker-compose.yml:
#   image: ghcr.io/paperless-ngx/paperless-ngx:3.0.0   # stable-Tag, nicht :latest

docker compose pull webserver
docker compose up -d --force-recreate webserver
```

**Nach erstem Start:**

- Tantivy-Index wird neu aufgebaut (Zeit, CPU-Last)
- Task-Historie ist leer
- Sessions/Tokens ungültig wenn `PAPERLESS_SECRET_KEY` rotiert wurde

### 2.5 Test-Checkliste nach v3

- [ ] Normaler PDF-Consume → pre_consume → post_consume → Tags/CF/Storage
- [ ] paper.manager Login (Authentik), Doc-Review, Proxy preview/thumb
- [ ] Pipeline-Notiz wird geschrieben und ersetzt
- [ ] `legacy-tasks-summary.sh` (nach Anpassung) funktioniert
- [ ] Suche in Paperless UI (Tantivy-Syntax für Notizen: `notes.note:`)
- [ ] Optional: `PAPERLESS_TRUSTED_PROXIES` wenn Login 403 hinter nginx

### 2.6 Rollback

```bash
# compose auf 2.20.15 zurück, DB-Backup restore falls Migration fehlschlägt
image: ghcr.io/paperless-ngx/paperless-ngx:2.20.15
```

v3-Migration ist destruktiv (frische DB-Migrations) — **Backup vorher ist Pflicht**.

---

## Was wäre, wenn wir **heute** (24.06.2026) auf v3 gingen?

| Aspekt | Bewertung |
|--------|-----------|
| v3 stable | ✗ Nur Beta (`3.0.0-beta.rc1`) |
| Legacy offen | ✗ ~mehrere NAS-Ordner + missing-Queue |
| `:latest`-Risiko | Hoch — könnte bei pull schon v3 ziehen |
| Duplikat-/Task-Verhalten | Ungetestet mit euren Legacy-Skripten |
| Paperless AI vs. Classifier | Strategische Überlappung, kein Blocker |
| Aufwand | Hoch — Env, DBENGINE, OCR, API, Skripte, Re-Test alles |

**Urteil:** Heute nur **Phase 0** (prüfen + pinnen). Legacy fertig, dann v3 stable abwarten.

---

## Kurz-Referenz Breaking Changes (Classifier-relevant)

Offizieller Guide: [migration-v3.md](https://github.com/paperless-ngx/paperless-ngx/blob/beta/docs/migration-v3.md)

1. Upgrade nur von **2.20.15**
2. `PAPERLESS_SECRET_KEY` Pflicht
3. `PAPERLESS_DBENGINE` Pflicht (Postgres)
4. OCR/Archiv entkoppelt (`skip` entfällt)
5. Consumer-Settings umbenannt/vereinheitlicht
6. Duplikate standardmäßig erlaubt
7. API v9/v10, Default v10 ohne Accept-Header
8. Tasks paginiert, `task_type` statt `task_name`
9. Whoosh → Tantivy (automatischer Rebuild)
10. Dokument-Versionen (API kompatibel, `find_pdf` über `DOCUMENT_SOURCE_PATH` ok)
11. Verschlüsselung entfernt (bei euch n/a)
12. pyzbar in Paperless entfernt (euer `pre_consume_qr.py` unberührt)

---

## Nächste konkrete Schritte (CT 121)

```bash
# 1. Repo aktualisieren
cd /opt/paperless-ngx-classifier && git pull

# 2. Version prüfen
./scripts/paperless-version-check.sh

# 3. Falls nötig: Pin in /opt/paperless/docker-compose.yml setzen (2.20.15)
#    dann: cd /opt/paperless && docker compose pull webserver && docker compose up -d --force-recreate webserver

# 4. Legacy fortsetzen (Phase 1)
/opt/paperless-scripts/legacy-nas-sha256.sh missing
# … import-loop oder legacy-one-batch.sh

# 5. v3 erst wenn stable + Legacy fertig — diese Datei Phase 2
```
