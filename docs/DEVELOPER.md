# paper.manager / paperless-ngx-classifier — Developer Guide

**Stand:** Juli 2026 · UI `2.47` · BE `2.35` · Pipe `12.44`

Benutzer-Doku: [`Benutzerhandbuch_paper_manager.md`](Benutzerhandbuch_paper_manager.md)

---

## 1. Architektur

```
Scanner / consume/
  ↓
pre_consume.sh          → ocrmypdf, optional Barcode-Split (Paperless built-in)
pre_consume_qr.py       → Swiss QR-Bill (SPC) → Sidecar-Metadaten
  ↓
Paperless-NGX           → OCR, Archivierung
  ↓
post_consume.py         → Vision, RAG, LLM, deterministisches Routing, Brillenpass-Trigger
  ↓
correspondent_manager   → FastAPI :8100, Review-Queues, Paperless-API-Proxy
paper_manager_ui.html   → SPA ohne Build-Step
```

**Deploy-Ziel (Produktion CT 121):** `/opt/paperless-scripts/` via `scripts/deploy-to-ct121.sh`

**Repo-Klon auf Server:** `/opt/paperless-ngx-classifier`

---

## 2. Verzeichnis & Module

| Pfad | Rolle |
|---|---|
| `post_consume.py` | Haupt-Pipeline, `maybe_queue_brillenpass()`, Paperless-API |
| `pre_consume.sh` / `pre_consume_qr.py` | Pre-Consume (QR-Bill) |
| `correspondent_manager_app.py` | FastAPI-Backend, Auth-Middleware, REST-API |
| `paper_manager_ui.html` | Frontend (eine Datei) |
| `brillenpass_parser.py` | Parser-Registry, Auto-Detect, Merge, Dedup-Helfer |
| `brillenpass_runner.py` | CLI/API: Dokument nachträglich durch Brillenpass-Pipeline |
| `legacy_split_by_qr.py` | QR-Metadaten-Split (NAS-Legacy), per Seite pdf2image+pyzbar |
| `document_date.py` | Belegdatum-Extraktion/Validierung |
| `training/*.example.json` | Schema-Beispiele (keine Live-Daten im Repo) |
| `tests/` | pytest (Parser, Fielmann, …) |

Konfiguration live auf dem Server: `/opt/paperless-scripts/training/` und `/opt/paperless/.env`

---

## 3. Versionierung

Drei unabhängige Versionsnummern — Details: [`VERSIONING.md`](VERSIONING.md)

| Konstante | Datei |
|---|---|
| `UI_VERSION` | `paper_manager_ui.html` **und** `correspondent_manager_app.py` (sync!) |
| `__version__` | `correspondent_manager_app.py` |
| `POST_CONSUME_VERSION` | `post_consume.py` |

Nach Änderungen: `git pull && ./scripts/deploy-to-ct121.sh` auf CT 121, Browser Hard-Refresh.

---

## 4. Authentifizierung (paper.manager)

Middleware: `require_paperless_session` in `correspondent_manager_app.py`

| Pfad | Verhalten |
|---|---|
| `/api/proxy/*` | Kein Auth (Backend nutzt `PAPERLESS_TOKEN`) |
| `/api/*` | `PAPER_MANAGER_TOKEN` (Header) **oder** gültige Paperless-`sessionid` |
| Sonst (HTML) | Session OK → ausliefern; sonst Redirect zu Paperless-Login |

**Host-aware Session-Check (ab BE 2.35):**

`_effective_paperless_url(request)`:

- Host ist **IP** → `http://<IP>:8000`
- Host ist **Domain** → `https://<domain>` (via `X-Forwarded-Proto`)
- `localhost` → `PAPERLESS_URL` aus `.env`

Session wird gegen diese URL **und** Fallback `PAPERLESS_INTERNAL_URL` per `GET /api/profile/` validiert.

**Häufiger Fehler:** Zugriff per `192.168.x.x:8100`, aber `PAPERLESS_URL` zeigt auf Domain → vor 2.35: `401 Nicht authentifiziert` auf API-POSTs (z. B. Legacy QR-Split).

---

## 5. Brillenpass — Entwickler

### 5.1 Datenfluss

```
post_consume.maybe_queue_brillenpass()
  → corr_brillenpass_parsers(corr_entry)     # Vendor → Parser-Kandidaten
  → looks_like_brillenpass_any()             # OCR + Vision-Heuristik
  → parse_brillenpass_auto()                 # ein Parser, kein Multi-Merge
  → vision_brillenpass_analyze()             # Bild + OCR
  → merge_brillenpass()
  → write_pending_brillenpass()              # pending_brillenpass.jsonl
```

Freigabe: `POST /api/brillenpass-review/{index}` → `brillenpaesse.json`, optional Dedup.

### 5.2 Parser-IDs (Naming: `{vendor}_{format}`)

| ID | Format | Funktion |
|---|---|---|
| `fielmann_rechnung` | rechnung | `parse_fielmann_brillenpass()` |
| `fielmann_brillenpass` | brillenpass | `parse_fielmann_pass()` |
| `mcoptic_rechnung` | rechnung | `parse_mcoptic_rechnung()` |
| `mcoptic_brillenpass` | brillenpass | `parse_mcoptic_pass()` |
| `augenarzt_verordnung` | verordnung | `parse_augenarzt()` |
| `optik_meyer_rechnung` | rechnung | `parse_optik_meyer_moehlin()` |

**Aliases** (Legacy-Config): `PARSER_ALIASES` in `brillenpass_parser.py` (`fielmann` → `fielmann_rechnung`, …).

### 5.3 Vendor-Konfiguration (`correspondents.json`)

```json
"brillenpass": {
  "aktiv": true,
  "vendor": "mcoptic",
  "typische_begriffe": ["McOptic", "Quittung"]
}
```

`corr_brillenpass_parsers()`:

1. `vendor` gesetzt → `VENDOR_PARSERS[vendor]`
2. Sonst explizite `parsers[]` — bei einem Optiker → alle Formate dieses Vendors
3. Normalisierung via `normalize_parser_name()`

### 5.4 Auto-Detect

`detect_parser(ocr_text, allowed=..., dokumenttyp_visuell=..., vision_meta=...)`

- Score pro Kandidat via `_DETECTORS[name](text)`
- Boost: `_vision_format_boost()`, `_vision_layout_boost()`
- Bester Score > 0 gewinnt

`parse_brillenpass_auto()` ruft **genau einen** Parser auf (kein Merge mehr aus Rechnung+Pass im selben Dokument).

### 5.5 Dedup bei Freigabe

`find_brillenpass_period_duplicate()` — gleiche Person, gleicher Korrespondent, `gueltig_ab` innerhalb `BRILLENPASS_DEDUP_DAYS` (Default 21).

`merge_brillenpass_version()` reichert bestehende Version an (`deduped: true` in API-Response).

### 5.6 Neuen Parser hinzufügen

1. Parse-Funktion + `_bp_base(parser_id, …)` in `brillenpass_parser.py`
2. `_detect_*()` Heuristik
3. Einträge in `PARSER_LABELS`, `PARSER_FORMAT`, `PARSER_VENDOR`, `VENDOR_PARSERS`, `_PARSERS`, `_DETECTORS`
4. Test in `tests/test_brillenpass_parsers.py`
5. Beispiel in `training/correspondents.example.json`
6. UI lädt Parser via `GET /api/brillenpass/parsers`

### 5.7 API (Auszug)

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/brillenpass` | Übersicht pro Person |
| GET | `/api/brillenpass/parsers` | Parser + Vendors für UI |
| POST | `/api/brillenpass/parse` | Dry-parse (text oder document_id) |
| POST | `/api/brillenpass/manual` | Manuelle Erfassung → Queue |
| POST | `/api/brillenpass/trigger/{doc_id}` | Nachträgliche Pipeline (async, Vision ~1–2 Min) |
| GET | `/api/brillenpass/trigger-status/{doc_id}` | Job-Status für UI-Polling (`running`/`done`/`error`) |
| GET | `/api/brillenpass-review` | Offene Reviews |
| POST | `/api/brillenpass-review/{index}` | Freigeben/Ablehnen |

CLI: `python brillenpass_runner.py <doc_id> [--parser mcoptic_brillenpass] [--force]`

### 5.8 Nachträglicher Trigger & Betrieb

- **Preflight (sync):** Dokument, OCR, Korrespondent, Person; ohne `force` Abbruch wenn Dok bereits in `pending_brillenpass.jsonl`.
- **Vision (async):** läuft in `_BG_EXECUTOR` (eigener Thread-Pool) — blockiert HTTP-Thread-Pool nicht (`_run_brillenpass_bg`).
- **UI:** pollt `trigger-status` alle 3s, max. ~2,5 Min; zeigt echte Fehler statt sofortigem Erfolg.

**Diagnose auf CT121:**

```bash
systemctl status correspondent-manager
journalctl -u correspondent-manager -n 80 --no-pager
curl -s http://127.0.0.1:8100/health
grep '"document_id": 3563' /opt/paperless-scripts/training/audit_log.jsonl | grep brillenpass | tail -5
```

**Bekannte Fallstricke:**

| Symptom | Ursache |
|---|---|
| `JSON.parse` / HTTP 502 | Sync Vision > Proxy-Timeout (alt, vor async) |
| `'bool' object has no attribute 'get'` | Bug in `diagnose_brillenpass_extraction` (fix `57de419`) |
| GET `/` hängt, 0 Bytes | Default-Thread-Pool erschöpft (Vision + sync Routes) — Fix: `_BG_EXECUTOR` + UI-Cache (`2.38`/`2.76`) |
| s1 leer, s2 ok | McOptic-OCR-Parser trifft nicht — nur Vision liefert Werte |
| Kein Review trotz merged | `write_pending` Dedup oder Crash nach s2 |

Ausführlicher Handoff: [BRILLENPASS_HANDOFF.md](BRILLENPASS_HANDOFF.md).

---

## 6. Legacy QR-Split — Entwickler

### 6.1 Unterschied zu anderen QR-Mechanismen

| Modul | QR-Typ | Wann |
|---|---|---|
| `pre_consume_qr.py` | Swiss QR-Bill `SPC` | Neuer Scan, Zahlungsdaten |
| Paperless Barcodes | PATCHT / ASN | Neuer Scan, Trennseiten |
| `legacy_split_by_qr.py` | `060102_Kategorie_Person` | Nachträglich, NAS-Altbestand |

Port des Bash-Scripts `tsa_barcode_split_function.sh`: Ghostscript 600 dpi → zbar (QR only) → pdftk-ähnliche Seitenlogik.

### 6.2 API

`POST /api/legacy-split/trigger/{doc_id}`

Body (JSON):

```json
{
  "dry_run": true,
  "regex": "^[0-9]{6}_[^\\s]+$",
  "consume_dir": "/mnt/paperless-data/consume"
}
```

- `dry_run: true` → Vorschau `{pages, splits: [{barcode, from_page, to_page}]}`
- `dry_run: false` → schreibt `ocrscan_{barcode}_{basename}_p{von}_bis_p{bis}.pdf` nach `consume_dir`

Env: `PAPERLESS_CONSUME_DIR`, `LEGACY_SPLIT_QR_REGEX`

### 6.3 Abhängigkeiten

- `pdf2image` (poppler)
- `pyzbar` (oder Subprocess `zbarimg`)
- Schreibrecht auf `PAPERLESS_CONSUME_DIR`

---

## 7. Wichtige `.env`-Variablen

| Variable | Zweck |
|---|---|
| `PAPERLESS_TOKEN` / `PAPERLESS_API_TOKEN` | Backend → Paperless API |
| `PAPERLESS_URL` | Kanonische URL (Domain) |
| `PAPERLESS_INTERNAL_URL` | Container-zu-Container, Session-Fallback |
| `PAPER_MANAGER_TOKEN` | Optional: API ohne Browser-Session |
| `PAPERLESS_CONSUME_DIR` | Legacy QR-Split Ziel |
| `LEGACY_SPLIT_QR_REGEX` | QR-Metadaten-Regex |
| `BRILLENPASS_DEDUP_DAYS` | Perioden-Dedup (Default 21) |
| `BRILLENPAESSE_JSON` | Gespeicherte Versionen |
| `PENDING_BRILLENPASS_JSONL` | Review-Queue |

Vollständig: `.env.example`

---

## 8. Tests & lokale Entwicklung

```bash
cd paperless-ngx-classifier
python -m pytest tests/test_brillenpass_parsers.py tests/test_brillenpass_fielmann.py -q
```

Backend lokal (ohne Paperless):

```bash
export PAPERLESS_TOKEN=...
export CORRESPONDENTS_JSON=training/correspondents.example.json
uvicorn correspondent_manager_app:app --host 0.0.0.0 --port 8100
```

UI: `paper_manager_ui.html` wird vom FastAPI-Root ausgeliefert.

---

## 9. Deploy-Checkliste

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/deploy-to-ct121.sh
grep -m1 POST_CONSUME_VERSION /opt/paperless-scripts/post_consume.py
grep -m1 UI_VERSION /opt/paperless-scripts/correspondent_manager_app.py
systemctl restart correspondent-manager   # falls systemd-Unit
```

Sidebar: `UI v2.47 | be v2.35 | pipe v12.44`

---

## 10. Private Doku (Git `/doku`)

Spiegel für CT-121-Betrieb:

```
doku/pve2/vm/121-paperless/Doku/docs/
  Benutzerhandbuch_paper_manager.md
  DEVELOPER.md
  LEGACY_IMPORT.md
  VERSIONING.md
  README.de.md
  paperless-restore-checkliste.md
```

Nach Repo-Änderungen: Classifier-Docs aktualisieren, dann in `/doku` synchron halten (gleicher Inhalt, VM-spezifische Pfade in LEGACY_IMPORT verweisen auf `ct121-nfs-fix.md`).
