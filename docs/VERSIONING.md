# Versionierung (paper.manager + Pipeline)

Drei unabhängige Versionsnummern — in der Sidebar und auf dem Home-Tab sichtbar (`/api/config` → `versions`).

| Komponente | Konstante | Datei(en) |
|---|---|---|
| **UI** | `UI_VERSION` | `paper_manager_ui.html` **und** `correspondent_manager_app.py` (immer synchron!) |
| **Backend** | `__version__` | `correspondent_manager_app.py` |
| **Pipeline** | `POST_CONSUME_VERSION` | `post_consume.py` (auch Dateikopf-Kommentar anpassen) |

## Wann hochzählen?

**Immer beim Commit**, wenn die Änderung die jeweilige Komponente betrifft — nicht erst auf Nachfrage.

| Komponente | Hochzählen bei | Beispiel |
|---|---|---|
| UI | Layout, Formulare, Tabs, clientseitige Logik, neue Felder in der Review-UI | `2.21` → `2.22` |
| BE | API-Endpunkte, Review-Aktionen, Queues, serverseitige Fixes in `correspondent_manager_app.py` | `2.9` → `2.10` |
| Pipe | Klassifizierung, Custom Fields, Routing, Tags, Pending-Logik in `post_consume.py` / `pre_consume*` | `12.15` → `12.16` |

- **Nur Bugfix** in einer Komponente → nur diese Komponente +1 (Patch-Stelle).
- **Feature über mehrere Schichten** → jede betroffene Komponente +1.
- **Reine Doku** → keine Versionsänderung.
- **`pre_consume.sh` / `pre_consume_qr.py`** → nur bei Änderungen dort (eigene `# VERSION` / `__version__`).

## Pflichten beim Bump

1. Konstante **und** Kurzkommentar in derselben Zeile aktualisieren (`# 2.10: …`).
2. `UI_VERSION` in **beiden** Dateien identisch halten.
3. Bei Pipeline: ersten Docstring-Zeilen in `post_consume.py` (`v12.x`) mitziehen.
4. Bei nutzerrelevanten Änderungen: `docs/Benutzerhandbuch_paper_manager.md` + README (`.de` / EN) prüfen.
5. Commit-Message kann Versionen erwähnen, muss aber nicht.

## Prüfen nach Deploy

```bash
curl -s http://localhost:8100/api/config | python3 -m json.tool | grep -A6 versions
```

Sidebar sollte `UI v… | be v… | pipe v…` zeigen — bei Abweichung Hard-Refresh (`Ctrl+Shift+R`).
