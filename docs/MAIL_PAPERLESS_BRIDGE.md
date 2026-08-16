# Mail → Paperless Bridge

Skript: `scripts/mail-paperless-bridge.py`

## Idee

1. Mails in einen **IMAP-Ordner** legen (z. B. `Paperless`) — manuell oder per Mail-Helper-Auto-Rule «verschieben nach Ordner».
2. Bridge (Cron/Timer auf **CT 121**) liest den Ordner, baut **pro Mail ein PDF** (Body + Anhänge), legt es atomisch in `consume/`.
3. Paperless consumer → `pre_consume` → `post_consume` → corr.manager.

**Kein Legacy-QR nötig:** Jede Mail = **eine PDF-Datei** in `consume/`. Paperless verarbeitet jede Datei als separates Dokument — wie einzelne Scan-Jobs.

## Ein Dokument pro Mail

Reihenfolge im zusammengefügten PDF:

1. **Deckblatt** (Betreff, Absender, Datum, IMAP-UID, Body-Text)
2. **Anhänge** (PDF-Seiten angehängt; Bilder → PDF; Office/sonstiges wird geloggt und übersprungen)

Inline-CID-Bilder (Logos) werden standardmäßig ignoriert (`MAIL_BRIDGE_SKIP_INLINE=true`).

## Installation (CT 121)

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/deploy-to-ct121.sh   # oder: cp scripts/mail-paperless-bridge.py /opt/paperless-scripts/

/opt/paperless-scripts/venv/bin/pip install pypdf reportlab pillow

sudo cp scripts/mail-paperless-bridge.env.example /etc/mail-paperless-bridge.env
sudo chmod 600 /etc/mail-paperless-bridge.env
# Werte anpassen

sudo mkdir -p /var/lib/mail-paperless-bridge

# Test
/opt/paperless-scripts/venv/bin/python3 /opt/paperless-scripts/mail-paperless-bridge.py \
  --env /etc/mail-paperless-bridge.env --dry-run -v

# Timer
sudo cp scripts/mail-paperless-bridge.service.example /etc/systemd/system/mail-paperless-bridge.service
sudo cp scripts/mail-paperless-bridge.timer.example /etc/systemd/system/mail-paperless-bridge.timer
sudo systemctl daemon-reload
sudo systemctl enable --now mail-paperless-bridge.timer
```

## Mail Helper anbinden

Auto-Rule Beispiel:

- **Bedingung:** Ordner = INBOX + Absender enthält `rechnung@…` (oder Tag)
- **Aktion:** `move_to_folder` → `Paperless`

Oder manuell in GMX/Webmail in Ordner `Paperless` verschieben.

## Nach dem Export

- Mit `MAIL_BRIDGE_IMAP_EXPORT_FOLDER`: Mail wird dorthin **verschoben** (COPY + Löschen in Quelle)
- Ohne Export-Ordner: Mail bleibt, wird als **gelesen** markiert
- UID in State-Datei → kein Doppel-Export

## Spätere Erweiterung

- Export direkt aus Mail-Helper-DB (Service-Token) statt IMAP — sinnvoll wenn Ordner-Sync nicht reicht
- Office-Anhänge via LibreOffice headless → PDF
