# paperless-ngx-classifier — Installationsanleitung

Diese Anleitung beschreibt die Erstinstallation auf einem frischen System.
Für Wiederherstellung nach Ausfall → siehe `paperless-restore-checkliste.md`.

## Voraussetzungen

| Komponente | Mindestversion | Hinweis |
|---|---|---|
| Paperless-NGX | v2.x | Läuft, Docker, API erreichbar |
| Ollama | aktuell | Separater Server empfohlen (GPU) |
| Python | 3.11+ | Auf dem Paperless-Host |
| Debian/Ubuntu | 12/24.04 | Andere Distros möglich, nicht getestet |

### Empfohlene Hardware für Ollama
- **Vision-Modell** `qwen2.5vl:7b`: min. 16 GB VRAM/RAM
- **LLM** `llama3.3:70b`: min. 64 GB RAM (CPU-Inference möglich, langsamer)
- Getestet: GMKtec EVO mit AMD Ryzen AI Max+ 395, 128 GB RAM

---

## Schritt 1 — Ollama-Modelle laden

Auf dem Ollama-Server:

```bash
ollama pull qwen2.5vl:7b
ollama pull llama3.3:70b
ollama pull bge-m3

# Testen:
ollama list
curl http://localhost:11434/api/tags
```

---

## Schritt 2 — Custom Fields in Paperless anlegen

In Paperless: **Admin → Custom Fields → Hinzufügen**

Felder in dieser Reihenfolge anlegen (IDs werden automatisch vergeben — notieren für `.env`):

| Feldname | Typ | Optionen |
|---|---|---|
| CHF | Monetär | — |
| Rechnungsnummer | Text | — |
| Kundennummer | Text | — |
| QR-Referenz | Text | — |
| Fällig am | Datum | — |
| Status | Auswahl | Optionen: `Offen`, `Bezahlt` |
| Policennummer | Text | — |
| Auto-Kennzeichen | Auswahl | Optionen: je nach Fahrzeugen in family.json |
| Bezahlt am | Datum | — |
| Gescannt am | Datum | — |

> Die IDs aus Paperless (sichtbar in der URL beim Bearbeiten) werden in `.env` als `CF_*_ID` eingetragen.

---

## Schritt 3 — Scripts deployen

```bash
# Verzeichnis anlegen
mkdir -p /opt/paperless-scripts/training
mkdir -p /opt/paperless-scripts/logs

# Repository klonen
git clone https://github.com/DEIN-USER/paperless-ngx-classifier.git /tmp/classifier

# Scripts kopieren
cp /tmp/classifier/post_consume.py        /opt/paperless-scripts/
cp /tmp/classifier/pre_consume.sh         /opt/paperless-scripts/
cp /tmp/classifier/pre_consume_qr.py      /opt/paperless-scripts/
cp /tmp/classifier/correspondent_manager_app.py /opt/paperless-scripts/
cp /tmp/classifier/paper_manager_ui.html  /opt/paperless-scripts/

# Ausführbar machen
chmod +x /opt/paperless-scripts/pre_consume.sh
chmod +x /opt/paperless-scripts/post_consume.py

# Training-Dateien von Beispielen initialisieren
cp /tmp/classifier/training/family.example.json           /opt/paperless-scripts/training/family.json
cp /tmp/classifier/training/document_types.example.json   /opt/paperless-scripts/training/document_types.json
cp /tmp/classifier/training/manifest.example.json         /opt/paperless-scripts/training/manifest.json
cp /tmp/classifier/training/correspondents.example.json   /opt/paperless-scripts/training/correspondents.json

# Leere Queue-Dateien anlegen
touch /opt/paperless-scripts/training/pending_correspondents.jsonl
touch /opt/paperless-scripts/training/document_review_queue.jsonl
touch /opt/paperless-scripts/training/audit_log.jsonl
echo "uncertain" > /opt/paperless-scripts/training/pending_mode.txt
```

---

## Schritt 4 — Python-Venv + Abhängigkeiten

```bash
cd /opt/paperless-scripts
python3 -m venv venv

venv/bin/pip install --upgrade pip
venv/bin/pip install \
    fastapi \
    uvicorn \
    requests \
    python-multipart \
    pdf2image \
    pyzbar \
    pillow \
    python-dotenv
```

---

## Schritt 5 — .env konfigurieren

```bash
cp /tmp/classifier/.env.example /opt/paperless/.env
nano /opt/paperless/.env
```

Mindestens folgende Werte anpassen:

```bash
# Paperless
PAPERLESS_URL=https://paperless.example.com
PAPERLESS_INTERNAL_URL=http://localhost:8000
PAPERLESS_TOKEN=DEIN_PAPERLESS_API_TOKEN
PAPERLESS_API_TOKEN=DEIN_PAPERLESS_API_TOKEN
PAPERLESS_API_URL=http://localhost:8000/api

# Ollama
OLLAMA_BASE_URL=http://192.168.x.x:11434
OLLAMA_MODEL_VISION=qwen2.5vl:7b
OLLAMA_MODEL_LLM=llama3.3:70b
OLLAMA_MODEL=llama3.3:70b

# Berechtigungen (IDs aus Paperless Admin → Gruppen)
PAPERLESS_OWNER_ID=1
PAPERLESS_VIEW_GROUP_IDS=1
PAPERLESS_CHANGE_GROUP_IDS=1

# Custom Fields (IDs aus Schritt 2)
CF_BETRAG_ID=1
CF_RECHNUNGSNUMMER_ID=2
CF_KUNDENNUMMER_ID=3
CF_QR_REFERENZ_ID=4
CF_FAELLIG_AM_ID=5
CF_STATUS_ID=6
CF_POLICENNUMMER_ID=7
CF_KENNZEICHEN_ID=8
CF_BEZAHLT_AM_ID=9
CF_GESCANNT_AM_ID=10

# paper.manager API-Schutz (zufälligen Token generieren)
PAPER_MANAGER_TOKEN=REPLACE_WITH_RANDOM_TOKEN
```

> **Paperless API-Token erstellen:** Paperless → Admin → Tokens → Token hinzufügen

---

## Schritt 6 — family.json konfigurieren

Entweder direkt editieren:

```bash
nano /opt/paperless-scripts/training/family.json
```

```json
{
  "version": "1.0",
  "haushalt": {
    "name": "MeinHaushalt",
    "land": "CH",
    "sprache": "de"
  },
  "personen": [
    {
      "id": "person1",
      "anzeigename": "Person1",
      "ordner_prefix": "Person1"
    }
  ],
  "fahrzeuge": []
}
```

Oder nach dem Start über paper.manager → Familie-Tab pflegen (empfohlen).

---

## Schritt 7 — Paperless Consumer Scripts eintragen

In `/opt/paperless/.env` (Paperless-Konfiguration):

```bash
PAPERLESS_POST_CONSUME_SCRIPT=/opt/paperless-scripts/post_consume.py
PAPERLESS_PRE_CONSUME_SCRIPT=/opt/paperless-scripts/pre_consume.sh
PAPERLESS_CONSUMER_POLLING=10
PAPERLESS_CONSUMER_ENABLE_BARCODES=true
```

Paperless neu starten:

```bash
cd /opt/paperless
docker compose down && docker compose up -d
```

Testen ob Scripts erkannt werden:

```bash
docker compose logs webserver | grep "pre_consume\|post_consume"
```

---

## Schritt 7b — Paperless Classifier deaktivieren (Pflicht)

Dies ist ein kritischer Schritt der oft vergessen wird.

### Hintergrund

Paperless-NGX betreibt einen eigenen ML-Classifier parallel zu unserem Script.
Dieser läuft **vor** `post_consume.py` und beeinflusst den Dateinamen den das Script
als Kontext erhält. Das führt zu Fehlklassifizierungen.

### Symptome wenn dieser Schritt fehlt

- Dokumente landen im falschen Ordner obwohl Vision den richtigen Absender erkannt hat
- Dateiname in den Logs enthält einen falschen Korrespondenten-Namen
- Confidence ist mittel/tief obwohl das Dokument klar klassifizierbar wäre
- Re-konsumierte Dokumente werden noch schlechter klassifiziert als beim ersten Mal

### Massnahmen

**1. In `/opt/paperless/.env` ergänzen:**
```bash
PAPERLESS_TRAIN_TASK_CRON=disable
```

**2. Docker neu starten:**
```bash
cd /opt/paperless
docker compose down && docker compose up -d
```

**3. Alle Korrespondenten auf «Keine automatische Zuweisung» setzen:**
```bash
# Einmalig nach der Installation ausführen.
# correspondent_manager_app.py setzt matching_algorithm=0 bei neu angelegten
# Korrespondenten automatisch — bestehende müssen einmalig manuell zurückgesetzt werden.

curl -s "http://localhost:8000/api/correspondents/?page_size=100" \
  -H "Authorization: Token $TOKEN" | python3 -m json.tool | grep '"id"' | \
  grep -o '[0-9]*' | while read id; do
    curl -s -X PATCH "http://localhost:8000/api/correspondents/$id/" \
      -H "Authorization: Token $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"matching_algorithm": 0}' > /dev/null
    echo "Korrespondent $id → Keine Zuweisung"
done
```

**4. Gleiches für Tags und Dokumenttypen (empfohlen):**
```bash
# Tags
curl -s "http://localhost:8000/api/tags/?page_size=100" \
  -H "Authorization: Token $TOKEN" | python3 -m json.tool | grep '"id"' | \
  grep -o '[0-9]*' | while read id; do
    curl -s -X PATCH "http://localhost:8000/api/tags/$id/" \
      -H "Authorization: Token $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"matching_algorithm": 0}' > /dev/null
done

# Dokumenttypen
curl -s "http://localhost:8000/api/document_types/?page_size=100" \
  -H "Authorization: Token $TOKEN" | python3 -m json.tool | grep '"id"' | \
  grep -o '[0-9]*' | while read id; do
    curl -s -X PATCH "http://localhost:8000/api/document_types/$id/" \
      -H "Authorization: Token $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"matching_algorithm": 0}' > /dev/null
done
```

---

## Schritt 8 — systemd Units einrichten

### correspondent-manager (paper.manager Backend)

```bash
cat > /etc/systemd/system/correspondent-manager.service << 'EOF'
[Unit]
Description=paper.manager — Paperless-NGX Review UI
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/paperless-scripts
EnvironmentFile=/opt/paperless/.env
ExecStart=/opt/paperless-scripts/venv/bin/uvicorn correspondent_manager_app:app \
    --host 0.0.0.0 --port 8100 --workers 1
Restart=on-failure
RestartSec=5
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now correspondent-manager
systemctl status correspondent-manager --no-pager
```

### Backup-Timer (optional, empfohlen)

```bash
# Service + Timer aus Repository kopieren:
cp /tmp/classifier/paperless-backup.service /etc/systemd/system/
cp /tmp/classifier/paperless-backup.timer   /etc/systemd/system/
cp /tmp/classifier/paperless-backup.sh      /opt/paperless-scripts/
chmod +x /opt/paperless-scripts/paperless-backup.sh

# Backup-Ziel in paperless-backup.sh anpassen (BACKUP_HOST, BACKUP_PATH)
nano /opt/paperless-scripts/paperless-backup.sh

systemctl daemon-reload
systemctl enable --now paperless-backup.timer
```

---

## Schritt 9 — nginx Reverse Proxy (optional)

paper.manager ist auf Port 8100 erreichbar. Für HTTPS + Authentik Forward Auth:

```nginx
# In nginx.conf / conf.d/paperless.conf:
location /corr-manager/ {
    # Authentik Forward Auth
    auth_request /outpost.goauthentik.io/auth/nginx;
    error_page 401 = @goauthentik_proxy_signin;
    auth_request_set $auth_cookie $upstream_http_set_cookie;
    add_header Set-Cookie $auth_cookie;

    proxy_pass http://192.168.x.x:8100/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

> Ohne Authentik: paper.manager ist nur via Paperless-Session-Cookie geschützt.
> Für produktiven Betrieb wird Authentik oder ein anderer Reverse-Proxy mit Auth empfohlen.

---

## Schritt 10 — Verify

```bash
# Backend erreichbar?
curl -s http://localhost:8100/api/config | python3 -m json.tool

# Versionen korrekt?
curl -s http://localhost:8100/api/config | python3 -m json.tool | grep -A5 versions

# Ollama erreichbar vom Paperless-Host?
curl -s http://OLLAMA_IP:11434/api/tags | python3 -m json.tool | grep name

# Test-Scan:
# Dokument in consume-Verzeichnis legen und Logs beobachten:
docker compose logs -f webserver | grep "post_consume\|pre_consume"

# paper.manager UI öffnen:
# http://SERVER_IP:8100
```

---

## Schnell-Diagnose

| Symptom | Ursache | Fix |
|---|---|---|
| post_consume.py startet nicht | Venv fehlt oder Abhängigkeiten | Schritt 4 wiederholen |
| Ollama Timeout | Modell nicht geladen oder falsche URL | `ollama list` + OLLAMA_BASE_URL prüfen |
| Custom Fields werden nicht gesetzt | CF_*_ID falsch | IDs in Paperless Admin prüfen |
| paper.manager nicht erreichbar | Service nicht gestartet | `systemctl status correspondent-manager` |
| 401 bei API-Calls | PAPER_MANAGER_TOKEN nicht gesetzt | .env prüfen, Service neu starten |
| Kennzeichen-Routing funktioniert nicht | family.json leer oder falsch | paper.manager → Familie → Fahrzeuge prüfen |
| Permissions-Fehler auf Dokumenten | Gruppen-IDs falsch | PAPERLESS_VIEW_GROUP_IDS in .env |

---

## Erste Schritte nach Installation

1. **paper.manager öffnen** → `http://SERVER_IP:8100`
2. **Familie konfigurieren** → Tab «Familie» → Haushalt + Personen + Fahrzeuge
3. **Ersten Scan** machen → QS-Modus EIN für vollständige Prüfung
4. **Korrespondenten Review** → neue Absender freigeben
5. **Manifest** → pending-Ordner ergänzen
6. **QS-Modus AUS** sobald System trainiert ist
