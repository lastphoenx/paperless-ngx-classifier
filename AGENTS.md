# paperless-ngx-classifier — Hinweise für KI-Assistenten (Cursor, Copilot, …)

> **Betreiber-Setup** (Prod-CT, interne Pfade, private Ops-Doku): nur im privaten Ops-Repo des Betreibers und im lokalen Cursor-Workspace mit `doku/`-Clone — **nicht** in diesem öffentlichen GitHub-Repo.

## Git & Branches

| Was | Branch |
|-----|--------|
| **Normal** (Fix, kleines Feature) | **`main`** — committen/pushen nur wenn Nutzer es verlangt |
| **Gross / unsicher / lange Testphase** | `feature/<kurzname>` — Merge wenn stabil; nicht auf Prod deployen bis Merge |

**Keine** einmaligen `fix/…`-Branches für kleine Änderungen.

### Standard-Workflow (main)

```powershell
cd paperless-ngx-classifier
git fetch origin
git checkout -f main
git pull origin main
# … ändern …
git add …
git commit -m "fix: …"   # nur wenn Nutzer committen verlangt
git push origin main     # nur wenn Nutzer pushen verlangt
```

**Nicht:** automatisch pushen; Push mit Approval nach Auto-review-Blockade ohne Nutzer-OK.

### Deploy (nur Betreiber — Agent führt nicht aus)

Siehe `docs/DEVELOPER.md` § Deploy und `scripts/deploy-to-ct121.sh`.

```bash
cd <REPO_CLONE_ON_SERVER> && git pull origin main && ./scripts/deploy-to-ct121.sh
```

Optional: `./scripts/deploy-to-ct121.sh --no-docker`

## Shell-Skripte (`scripts/*.sh`)

- Ausführbar im Git-Index: `git add --chmod=+x scripts/….sh` → Mode `100755`
- **Niemals** `chmod +x` auf dem Server nach `git pull` als Fix
- `.gitattributes`: `*.sh text eol=lf`

## Tests (lokal)

```powershell
python -m pytest tests/ -q
```

Kein Python lokal → Nutzer informieren; nicht auf Prod-Server testen.

## Secrets & Server

- Kein SSH / kein Remote-`docker exec` vom Agent
- Kein `grep` auf `.env` / `docker-compose.yml` mit Klartext-Werten

## Read-first (Links / Proxy / Auth)

**Symptom ≠ Umbau.** Vor dem ersten Edit:

1. `.env.example` — nur `PAPERLESS_URL`, `PAPERLESS_INTERNAL_URL`, `PAPERLESS_API_URL`
2. `url_links.py` — zentrale Link-Logik
3. `docs/DEVELOPER.md`
4. `docs/Benutzerhandbuch_paper_manager.md`

Dann Doku/Code zitieren — erst dann ändern. Detail: `.cursor/rules/urls-and-proxy.mdc`

### URL-Invarianten

| Thema | Regel |
|-------|--------|
| Ports | `:8100` paper.manager · `:8000` Paperless |
| iframe/Vorschau | `/api/proxy/document/{id}/…` — gleiche Origin wie UI |
| Neuer Tab PDF | Paperless `/api/documents/{id}/preview/` |
| Domain | `PAPERLESS_URL` |
| LAN-IP | Request-Host `:8000` / `:8100` |
| Neue Env-Keys | **Verboten** ohne Nutzerfreigabe |

## Pipeline

- UI: `paper_manager_ui.html` · Backend: `correspondent_manager_app.py` · Pipeline: `post_consume.py`
- Entwickler: `docs/DEVELOPER.md`
