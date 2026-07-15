# Versionierung (paper.manager + Pipeline)

**Paperless-NGX** (Docker-Image) ist unabhängig: Produktion **2.20.15** gepinnt — siehe [UPGRADE_V3.md](./UPGRADE_V3.md) und `./scripts/paperless-version-check.sh`.

Drei Classifier-Versionsnummern — in der Sidebar und auf dem Home-Tab sichtbar (`/api/config` → `versions`).

| Komponente | Konstante | Datei(en) |
|---|---|---|
| **UI** | `UI_VERSION` | `paper_manager_ui.html` **und** `correspondent_manager_app.py` (immer synchron!) |
| **Backend** | `__version__` | `correspondent_manager_app.py` |
| **Pipeline** | `POST_CONSUME_VERSION` | `post_consume.py` (auch Dateikopf-Kommentar anpassen) |

## Wann hochzählen?

**Immer beim Commit**, wenn die Änderung die jeweilige Komponente betrifft — nicht erst auf Nachfrage.

| Komponente | Hochzählen bei | Beispiel |
|---|---|---|
| UI | Layout, Formulare, Tabs, clientseitige Logik, neue Felder in der Review-UI | `2.22` → `2.23` |
| BE | API-Endpunkte, Review-Aktionen, Queues, serverseitige Fixes in `correspondent_manager_app.py` | `2.10` → `2.11` |
| Pipe | Klassifizierung, Custom Fields, Routing, Tags, Pending-Logik in `post_consume.py` / `pre_consume*` | `12.19` → `12.20` |

- **Nur Bugfix** in einer Komponente → nur diese Komponente +1 (Patch-Stelle).
- **Feature über mehrere Schichten** → jede betroffene Komponente +1.
- **Reine Doku** → keine Versionsänderung.
- **`pre_consume.sh` / `pre_consume_qr.py`** → nur bei Änderungen dort (eigene `# VERSION` / `__version__`).

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
```

`deploy-to-ct121.sh` kopiert **immer** `post_consume.py` (nicht nur UI/BE). Ohne das läuft auf dem Server weiter die alte Pipeline. Anschliessend wird `docker compose up -d --force-recreate webserver` ausgeführt (lädt `/opt/paperless/.env` neu — `restart` reicht nicht für neue `CF_*_ID`). Mit `--no-docker` überspringen.

Sidebar sollte `UI v… | be v… | pipe v…` zeigen — bei Abweichung Hard-Refresh (`Ctrl+Shift+R`).

## Aktuell (Stand Juli 2026)

| Komponente | Version | Kurz |
|---|---|---|
| UI | 3.15 | Legacy-Split `delete_source`, SWIFT-Felder, `switchMergeToNeu`-Fix |
| BE | 2.62 | Legacy-Split atomischer Publish, Identifikatoren SWIFT/Telefon |
| Pipe | 12.75 | Telefon (`phonenumbers`), SWIFT-Extraktion, BKB↔BLKB-Blacklist |
