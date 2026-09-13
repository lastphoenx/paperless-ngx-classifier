# Versionierung (paper.manager + Pipeline)

**Paperless-NGX** (Docker-Image) ist unabhängig: Ziel **3.1.2** — siehe [UPGRADE_V3.md](./UPGRADE_V3.md) und `./scripts/paperless-version-check.sh`.

Classifier-Versionsnummern — Sidebar (UI · be · pipe), Home-Tab inkl. **pre OCR / pre QR** (`/api/config` → `versions`).

| Komponente | Konstante | Datei(en) |
|---|---|---|
| **UI** | `UI_VERSION` | `paper_manager_ui.html` **und** `correspondent_manager_app.py` (immer synchron!) |
| **Backend** | `__version__` | `correspondent_manager_app.py` |
| **Pipeline (post)** | `POST_CONSUME_VERSION` | `post_consume.py` (auch Dateikopf-Kommentar anpassen) |
| **Pre OCR** | `# VERSION:` | `pre_consume.sh` (Zeile 3, Format `# VERSION: 1.6 — Kurzkommentar`) |
| **Pre QR** | `__version__` | `pre_consume_qr.py` |

## Für Agents (Cursor / CI)

**Bei jeder Code-Änderung** an einer der Dateien oben: **Versionsnummer derselben Komponente im selben Commit hochzählen** — nicht vergessen, nicht «nur kurz fixen».

| Datei geändert | Pflicht |
|---|---|
| `post_consume.py` | `POST_CONSUME_VERSION` + Docstring `post_consume_v12.x` |
| `pre_consume.sh` | `# VERSION: x.y` in Zeile 3 |
| `pre_consume_qr.py` | `__version__ = "x.y"` |
| `correspondent_manager_app.py` | `__version__` (und `UI_VERSION` nur wenn UI mitgeändert) |
| `paper_manager_ui.html` | `UI_VERSION` + synchron in `correspondent_manager_app.py` |

Nach Deploy prüfen: `/api/config` → `versions.pre_consume_sh` / `pre_consume_qr` (nicht `?`).

## Wann hochzählen?

**Immer beim Commit**, wenn die Änderung die jeweilige Komponente betrifft — nicht erst auf Nachfrage.

| Komponente | Hochzählen bei | Beispiel |
|---|---|---|
| UI | Layout, Formulare, Tabs, clientseitige Logik, neue Felder in der Review-UI | `3.18` → `3.19` |
| BE | API-Endpunkte, Review-Aktionen, Queues, serverseitige Fixes in `correspondent_manager_app.py` | `2.67` → `2.68` |
| Pipe (post) | Klassifizierung, Custom Fields, Routing, Tags, Pending-Logik in `post_consume.py` | `12.80` → `12.81` |
| Pre OCR | OCR/QR-Lock, ocrmypdf, Container-Pfade in `pre_consume.sh` | `1.6` → `1.7` |
| Pre QR | Swiss-QR-Parsing, pyzbar, Sidecar in `pre_consume_qr.py` | `1.0` → `1.1` |

- **Nur Bugfix** in einer Komponente → nur diese Komponente +1 (Patch-Stelle).
- **Feature über mehrere Schichten** → jede betroffene Komponente +1.
- **Reine Doku** → keine Versionsänderung.

## Pflichten beim Bump

1. Konstante **und** Kurzkommentar in derselben Zeile aktualisieren (`# 2.10: …`).
2. `UI_VERSION` in **beiden** Dateien identisch halten.
3. Bei Pipeline: ersten Docstring-Zeilen in `post_consume.py` (`v12.x`) mitziehen.
4. Bei nutzerrelevanten Änderungen: `docs/Benutzerhandbuch_paper_manager.md` + `docs/DEVELOPER.md` + README (`.de` / EN) prüfen.
5. Commit-Message kann Versionen erwähnen, muss aber nicht.

## Prüfen nach Deploy

```bash
cd /opt/paperless-ngx-classifier && git pull && ./scripts/deploy-to-ct121.sh
grep -m1 POST_CONSUME_VERSION /opt/paperless-scripts/post_consume.py
grep -m1 '^# VERSION' /opt/paperless-scripts/pre_consume.sh
grep -m1 __version__ /opt/paperless-scripts/pre_consume_qr.py
```

`deploy-to-ct121.sh` kopiert **immer** `post_consume.py`, `pre_consume.sh`, `pre_consume_qr.py`, `docs/Benutzerhandbuch_paper_manager.md` (optional `.docx`). Anschliessend `docker compose up -d --force-recreate webserver` (lädt `.env` neu). **Danach automatisch** `ensure-legacy-qr-deps.sh` (Container-Recreate löscht apt-Pakete wie libzbar). Mit `--no-docker` entfällt beides.

Sidebar: `UI v… | be v… | pipe v…` — Home-Tab: zusätzlich **pre OCR / pre QR**. Hard-Refresh (`Ctrl+Shift+R`).

## Aktuell (Stand September 2026)

| Komponente | Version | Kurz |
|---|---|---|
| UI | 3.25 | PmLinks zentral; Handbuch-iframe via Proxy; breitere Vorschau |
| BE | 2.73 | url_links.py; Proxy Binary-Headers; links in /api/config |
| Pipe | 12.82 | Identifikatoren: SWIFT nur mit Bank-Kontext, Tel nur gelabelt |
| Pre OCR | 1.6 | ocrmypdf + QR-Lock (unverändert) |
| Pre QR | 1.0 | Versionskennzeichnung init |

**Deploy:** `deploy-to-ct121.sh` ruft nach Container-Recreate automatisch `ensure-legacy-qr-deps.sh` auf — manuell nur bei Erstsetup oder libzbar-Fehler.
