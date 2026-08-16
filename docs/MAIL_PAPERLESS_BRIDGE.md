# Mail → Paperless Bridge

Skript: `scripts/mail-paperless-bridge.py` → Deploy nach `/opt/paperless-scripts/`

## Pfade auf CT 121 (Konvention wie Paperless)

| Was | Pfad |
|-----|------|
| Git-Clone | `/opt/paperless-ngx-classifier/` |
| Skripte (live) | `/opt/paperless-scripts/mail-paperless-bridge.py` |
| Python venv | `/opt/paperless-scripts/venv/` |
| **Konfiguration** | `/opt/paperless-scripts/mail-paperless-bridge.env` (chmod 600, **nicht** in Git) |
| State (verarbeitete UIDs) | `/opt/paperless-scripts/state/mail-paperless-bridge.json` |
| Paperless consume | `/mnt/paperless-data/consume/` |
| Paperless `.env` | `/opt/paperless/.env` (separat — nur Paperless-Container) |
| systemd Units | `/etc/systemd/system/mail-paperless-bridge.{service,timer}` |

> **Hinweis:** Frühere Doku nannte `/etc/mail-paperless-bridge.env` — auf CT 121 liegt alles Betriebsrelevante unter `/opt/`. Falls du schon `/etc/…` angelegt hast: `mv /etc/mail-paperless-bridge.env /opt/paperless-scripts/mail-paperless-bridge.env`

## Idee

1. Mails in einen **IMAP-Ordner** legen (z. B. `Paperless`) — manuell oder per Mail-Helper-Auto-Rule «verschieben nach Ordner».
2. Bridge (Timer auf **CT 121**) liest den Ordner, baut **pro Mail ein PDF** (Body + Anhänge), legt es atomisch in `consume/`.
3. Paperless consumer → `pre_consume` → `post_consume` → corr.manager.

**Kein Legacy-QR nötig:** Jede Mail = **eine PDF-Datei** in `consume/`.

## Ein Dokument pro Mail

1. **Deckblatt** (Betreff, Absender, Datum, IMAP-UID, Body-Text)
2. **Anhänge** (PDF angehängt; Bilder → PDF; Office wird geloggt und übersprungen)

Inline-CID-Bilder (Logos) standardmäßig ignoriert (`MAIL_BRIDGE_SKIP_INLINE=true`).

## Installation (CT 121)

Als **root** (CT hat kein `sudo`):

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/deploy-to-ct121.sh   # kopiert u.a. mail-paperless-bridge.py

/opt/paperless-scripts/venv/bin/pip install pypdf reportlab pillow

# Konfiguration (einmalig)
cp scripts/mail-paperless-bridge.env.example /opt/paperless-scripts/mail-paperless-bridge.env
chmod 600 /opt/paperless-scripts/mail-paperless-bridge.env
nano /opt/paperless-scripts/mail-paperless-bridge.env

mkdir -p /opt/paperless-scripts/state

# Test (dry-run)
/opt/paperless-scripts/venv/bin/python3 /opt/paperless-scripts/mail-paperless-bridge.py --dry-run -v

# systemd Timer (alle 15 min)
cp scripts/mail-paperless-bridge.service.example /etc/systemd/system/mail-paperless-bridge.service
cp scripts/mail-paperless-bridge.timer.example /etc/systemd/system/mail-paperless-bridge.timer
systemctl daemon-reload
systemctl enable --now mail-paperless-bridge.timer
systemctl list-timers mail-paperless-bridge.timer
```

## Mail Helper anbinden (CT 134)

Auto-Rule in Mail Helper:

- **Bedingung:** z. B. Tag oder Absender
- **Aktion:** `move_to_folder` → `Paperless`

Oder manuell in GMX/Webmail in Ordner `Paperless` verschieben.

## Nach dem Export

- Mit `MAIL_BRIDGE_IMAP_EXPORT_FOLDER`: Mail wird dorthin **verschoben**
- Ohne Export-Ordner: Mail bleibt, wird als **gelesen** markiert
- UID in State-Datei → kein Doppel-Export

## Troubleshooting

| Symptom | Prüfen |
|---------|--------|
| `Keine Mails in Paperless` | IMAP-Ordner existiert? Test-Mail reingelegt? `MAIL_BRIDGE_IMAP_FOLDER` exakt wie auf dem Server? |
| `IMAP SELECT fehlgeschlagen` | Ordnername bei GMX oft `Paperless` oder `INBOX/Paperless` — in Webmail nachschauen |
| PDF landet nicht in consume | `MAIL_BRIDGE_CONSUME_DIR`, Schreibrechte, `docker compose logs webserver` |
| Doppel-Export | State-Datei unter `/opt/paperless-scripts/state/` löschen nur wenn bewusst |

## Spätere Erweiterung

- Export aus Mail-Helper-DB (Service-Token) statt IMAP
- Office-Anhänge via LibreOffice headless → PDF
