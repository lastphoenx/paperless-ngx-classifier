# Deploy — vom Windows-Repo auf CT 121 (paper.manager)

Kurzablauf: **Cursor/Windows → `git commit` + `git push` → auf CT 121 `git pull` + Deploy-Skript**.

---

## 1. Auf Windows (Cursor / github_code)

Änderungen liegen in `paperless-ngx-classifier/` (eigenes Git-Repo).

```powershell
cd "C:\Users\tsant\OneDrive\Dokumente\vsc\github_code\paperless-ngx-classifier"
git status
git pull
git add -A
git commit -m "fix(security): proxy auth, legacy-split hardening, regex validation"
git push origin main
```

`doku/` ist ein **separates** Repo (`lastphoenx/doku`) — Vaultwarden-Doku etc. dort separat committen/pushen.

---

## 2. Auf Proxmox / CT 121 (Paperless)

Einmalig (falls noch nicht):

```bash
git clone git@github.com:lastphoenx/paperless-ngx-classifier.git /opt/paperless-ngx-classifier
```

**Jedes Update:**

```bash
pct enter 121
# oder: ssh root@<ct121-ip>

cd /opt/paperless-ngx-classifier
git pull origin main

# Scripts nach /opt/paperless-scripts kopieren + correspondent-manager neu starten
./scripts/deploy-to-ct121.sh --no-docker
```

`--no-docker` = nur corr.manager + Skripte, **ohne** Paperless-Container-Neustart (reicht für BE/UI-Security-Fixes).

Ohne Flag: zusätzlich `docker compose up -d --force-recreate webserver` (nur nötig bei `.env`/CF_*-Änderungen).

---

## 3. Verifikation (CT 121)

```bash
grep -m1 '__version__' /opt/paperless-scripts/correspondent_manager_app.py
# Erwartung: 2.60

systemctl status correspondent-manager --no-pager

# Proxy ohne Session → 401
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8100/api/proxy/document/1/preview/
```

Im Browser: paper.manager öffnen, Dokument-Review → Thumbnail/Vorschau muss noch laden (mit Login).

---

## 4. CVE-Check (pip-audit)

```bash
cd /opt/paperless-ngx-classifier
./scripts/dependency-audit.sh
```

(`deploy-to-ct121.sh` setzt `chmod +x` auf `scripts/*.sh`; `dependency-audit.sh` ist im Repo mit +x.)

Installiert `pip-audit` einmalig ins venv unter `/opt/paperless-scripts/venv`.

---

## 5. Doku-Sync (optional)

Repo `doku` enthält Kopien unter `pve2/vm/121-paperless/Doku/`. Optional:

```powershell
# Windows
cd ...\paperless-ngx-classifier\scripts
.\sync-to-121-doku.ps1
```

Oder manuell in `doku`-Repo committen nach `git pull` in beiden Repos.
