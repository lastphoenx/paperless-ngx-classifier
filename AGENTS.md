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

**Host:** Proxmox CT 121 (`paperless`). **Agent:** keine SSH — nur Befehle liefern.

| Pfad | Inhalt |
|------|--------|
| `/opt/paperless-ngx-classifier` | Git-Clone (pull + `deploy-to-ct121.sh`) |
| `/opt/paperless-scripts` | Live-Code (UI, `post_consume`, corr-manager-App) |
| `/opt/paperless` | Paperless `docker-compose.yml`, `.env` |
| `/usr/local/sbin/paperless-nfs-remount.sh` | NFS-Boot ( **nicht** Teil von `deploy-to-ct121.sh`) |

#### Standard (Pipeline + paper.manager + Paperless-Container)

```bash
cd /opt/paperless-ngx-classifier
git pull origin main
./scripts/deploy-to-ct121.sh
```

Das Skript: kopiert Dateien → `/opt/paperless-scripts` → **`systemctl restart correspondent-manager`** nur wenn der Service **active** ist (sonst keine Zeile im Log) → **`docker compose up -d --force-recreate webserver`** → `ensure-legacy-qr-deps.sh`.

Nach Deploy prüfen:

```bash
systemctl is-active correspondent-manager
curl -s -o /dev/null -w "paper.manager HTTP %{http_code}\n" http://127.0.0.1:8100/api/config
docker compose -f /opt/paperless ps webserver
grep POST_CONSUME_VERSION /opt/paperless-scripts/post_consume.py | head -1
```

Falls corr-manager nicht neu startete: `systemctl restart correspondent-manager`

#### Varianten

```bash
# Nur Scripts + corr-manager, kein Paperless-Container-Recreate
cd /opt/paperless-ngx-classifier && ./scripts/deploy-to-ct121.sh --no-docker

# Nur kopieren, kein systemctl/docker (selten)
./scripts/deploy-to-ct121.sh --no-restart
```

#### Nur Doku/NFS im Repo (kein App-Deploy nötig)

```bash
cd /opt/paperless-ngx-classifier && git pull origin main
# Kein deploy-to-ct121.sh — NFS-Skripte liegen unter scripts/*.example
```

Details: `docs/DEVELOPER.md` § Deploy · NFS: `docs/NFS_BOOT_RESILIENCE.md` · privat: `doku/pve2/vm/121-paperless/`

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
