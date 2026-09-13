# Paperless-NGX — Anpassungen die Updates überleben

Kurzrecherche (Stand 2026). Quellen: [Configuration](https://docs.paperless-ngx.com/configuration/), GitHub-Diskussionen #10356, #11995.

## Was Paperless offiziell unterstützt (update-sicher)

| Mechanismus | Zweck | Persistenz |
|-------------|--------|------------|
| **`PAPERLESS_APP_TITLE`** in `.env` | Name statt «Paperless-ngx» (Login, Titel) | `.env` + optional UI «General» |
| **`PAPERLESS_APP_LOGO`** | Logo-Pfad unter `/media/logo/…` (Dateiname muss `logo` enthalten) | Datei in `media/logo/` (Bind-Mount) |
| **UI Einstellungen → General** | Sprache, Theme, Sidebar — pro User in DB | PostgreSQL/SQLite |
| **Gespeicherte Ansichten (Saved Views)** | Eigene Dashboard-Kacheln, Filter, Links zu Dokumentlisten | DB, überlebt Updates |
| **`PAPERLESS_URL`**, OAuth, Gruppen | Auth, Redirects, Rechte | `.env` + DB |

Logo-Beispiel in `.env`:

```bash
PAPERLESS_APP_TITLE=Paperless Haushalt
PAPERLESS_APP_LOGO=/logo/haus-logo.svg
```

Datei ablegen: `{PAPERLESS_MEDIA_ROOT}/logo/haus-logo.svg` (im Container typisch unter `/usr/src/paperless/media/logo/`).

## Was **nicht** unterstützt wird

| Ansatz | Status |
|--------|--------|
| **`overrides.css` / `overrides.js`** in media | **Entfernt** — war altes Paperless, in Paperless-ngx **nie** offiziell (#10356, #11995) |
| **Welcome-Widget Text** («Paperless-ngx wurde erfolgreich gestartet») | **Hardcoded** in Angular (`welcome-widget.component.html`) — **kein** Env-Hook |
| **UI-Features ausblenden** (Suggestions etc.) | Nicht vorgesehen; nur Fork oder Browser-Extension |
| **Custom HTML auf Dashboard** | Kein Plugin-System |

Maintainer-Stellung (#11995): Realistische Optionen sind Fork, Browser-CSS, oder konkrete Upstream-PRs — kein per-Installation UI-Trimming.

## Empfehlung für unser Setup (CT121)

1. **Haushalt-Hinweise / Links / Handbuch** → **paper.manager** Start-Tab (`/#home`) — unser Code, eigenes Deploy, updates unabhängig von Paperless-Container.
2. **Optionaler Hinweis oben / im Tab-Titel** → `PAPER_MANAGER_HINT_TEXT` + `PAPER_MANAGER_HINT_BANNER` / `PAPER_MANAGER_HINT_TITLE` in `.env` (correspondent-manager liest mit).
3. **Paperless Branding** → `PAPERLESS_APP_TITLE` + optional Logo in `media/logo/`.
4. **Dashboard statt Welcome-Widget** → Nutzer kann Widget wegklicken (X); stattdessen **Saved View** «Offene Rechnungen» o.ä. auf Dashboard pinnen.
5. **Doku-Link** → Gespeicherte Ansicht oder Lesezeichen; kein eingebauter Custom-Text im Welcome-Widget.

Hinweis-Banner (paper.manager, `.env`):

```bash
PAPER_MANAGER_HINT_TEXT=Review: paper.manager — :8100 oder /corr-manager/
PAPER_MANAGER_HINT_BANNER=true   # Leiste oben in der UI
PAPER_MANAGER_HINT_TITLE=true    # Browser-Tab «paper.manager — …»
# Leer lassen oder TEXT weglassen = komplett aus
```

## CI/CD-tauglich (ohne Paperless-Fork)

- `.env` / Compose / Ansible: `PAPERLESS_APP_*`, Volumes für `data`, `media`, `export`
- `media/logo/` per Deploy-Skript befüllen (Git oder Artefakt)
- **Nicht** Paperless-Frontend patchen — paper.manager separat deployen (`deploy-to-ct121.sh`)
