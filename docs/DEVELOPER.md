# paper.manager / paperless-ngx-classifier — Developer Guide

**Stand:** März 2026 · UI `3.12` · BE `2.59` · Pipe `12.75`

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
| `handwriting_vision.py` | HTR-Profil-Routing, Pre-Resolution, Content-Strategie D |
| `htr_runner.py` | Nachträgliche HTR (CLI + async Jobs) |
| `image_crop.py` | PDF-Render, Trim/Horizontal-Bands für HTR |
| `schulbericht_vision.py` | Schulbericht HTR + Extract, Zeilen-Merge |
| `document_date.py` | Belegdatum-Extraktion/Validierung |
| `post_consume_runner.py` | Nachträgliche volle Pipeline (UI «Pipeline nachholen») |
| `iban_utils.py` | IBAN-Extraktion (OCR-Text), Modulo-97-Validierung, Formatierung |
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
| `/api/proxy/*` | Wie `/api/*`: `PAPER_MANAGER_TOKEN` **oder** gültige Paperless-`sessionid` |
| `/api/*` | `PAPER_MANAGER_TOKEN` (Header) **oder** gültige Paperless-`sessionid` |
| Sonst (HTML) | Session OK → ausliefern; sonst Redirect zu Paperless-Login |

**Host-aware Session-Check (ab BE 2.35):**

`_effective_paperless_url(request)`:

- Host ist **IP** → `http://<IP>:8000`
- Host ist **Domain** → `https://<domain>` (via `X-Forwarded-Proto`)
- `localhost` → `PAPERLESS_URL` aus `.env`

Session wird gegen diese URL **und** Fallback `PAPERLESS_INTERNAL_URL` per `GET /api/profile/` validiert.

**Häufiger Fehler:** Zugriff per `192.168.x.x:8100`, aber `PAPERLESS_URL` zeigt auf Domain → vor 2.35: `401 Nicht authentifiziert` auf API-POSTs (z. B. Legacy QR-Split).

**Produktion:** UI unter `https://paperless.santinel.li/corr-manager/` — nginx strippt Prefix; Frontend setzt `API_BASE = '/corr-manager/api'`.

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
| GET `/` hängt, 0 Bytes | SyntaxError in `brillenpass_parser` ab `57de419` — Import scheitert, Service tot; danach `2.40` |
| s1 leer, s2 ok | McOptic-OCR-Parser trifft nicht — nur Vision liefert Werte |
| Kein Review trotz merged | `write_pending` Dedup oder Crash nach s2 |
| «Keine Glaswerte» in Übersicht | Freigabe speicherte nur `fern`/`naehe`, nicht `messung` — Fix BE 2.59+; `repair_brillenpaesse.py` |

Ausführlicher Handoff: [BRILLENPASS_HANDOFF.md](BRILLENPASS_HANDOFF.md).

---

## 6. HTR (Handschrift) — Entwickler

### 6.1 Datenfluss (Consume)

```
post_consume: Vision (Baseline)
  → decide_htr_action()          # Pre-Resolution
  → run_htr_pipeline()           # wenn action=run_now
  → extract_htr_searchable_text() + build_htr_content_append(drop_ocr=…)
  → Paperless content + audit_log
```

| `HtrPreResolution.action` | Verhalten |
|---|---|
| `run_now` | HTR sofort (Profil aus Doctype / Korrespondent / Heuristik) |
| `defer` | Tag `pending_htr_decision`, Eintrag in `pending_htr_decision.jsonl` |
| `skip` | Kein HTR |

### 6.2 Konfiguration

| Datei | Inhalt |
|---|---|
| `training/htr_profiles.json` | Profile: `pipeline`, `crop_mode`, `dpi`, `horizontal_bands` |
| `training/document_types.json` | `htr_profile` pro Typ: `auto` \| `default` \| `schulbericht` \| `off` |
| `training/correspondents.json` | Optional `htr_profiles_by_document_type` |

Registry-Defaults in `handwriting_vision.py`; Live-Datei überschreibt.

### 6.3 Schulbericht-Pipeline

`analyze_schulbericht_two_stage()` in `schulbericht_vision.py`:

1. HTR pro Seite (optional horizontal bands via `image_crop.py`)
2. `clean_htr_lines()` — Junk/Dedup
3. Extract nur aus **Seite 1** (`transcript_for_metadata_extract`)
4. Content **Strategie D:** Metadaten-Kopf + `--- Seite N ---` Transkript (`extract_htr_searchable_text`)

### 6.4 API

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/api/htr/trigger/{doc_id}` | Nachträgliche HTR (async), Body: `{profile?}` |
| GET | `/api/htr/trigger-status/{doc_id}` | `running` / `done` / `error` |
| GET | `/api/htr/pending` | Offene `pending_htr_decision`-Einträge |

CLI: `python3 htr_runner.py <doc_id> [--profile schulbericht]`

Tests: `tests/test_htr_sanitize.py`

### 6.5 Deploy

`deploy-to-ct121.sh` kopiert `handwriting_vision.py`, `image_crop.py`, `htr_runner.py`, `schulbericht_vision.py`. Legt `htr_profiles.json` aus Example an wenn fehlend.

---

## 7. Korrespondenten & Identifikatoren

### 7.1 Platzhalter (`platzhalter: true`)

Synthetische Korrespondenten ohne echten Absender (z. B. `Gesundheit`, `Medien`, `Privat`). User legt sie selbst an.

| Schicht | Verhalten |
|---|---|
| `correspondents.json` | Feld `platzhalter: bool` (Default `false`) |
| UI | Checkbox beim Edit, Badge in Liste/Picker, Filter, Batch (`POST /api/correspondents/batch-platzhalter`) |
| `post_consume.py` | `_is_corr_platzhalter()` — überspringt Eintrag bei `_resolve_corr_entry`, Fuzzy, Identifikator-Match |

Platzhalter erscheinen im Dokument-Review-Picker, werden aber **nie** automatisch zugeordnet.

### 7.2 Korrespondenten-Picker (UI 3.11)

Ersetzt natives `<select>` in Dokument-Review und Pending-Zuweisung. Zeigt `badge-kuerzel` und `badge-platzhalter` als echte HTML-Badges (durchsuchbar).

### 7.3 Identifikatoren & IBAN (`iban_utils.py`)

```json
"identifikatoren": {
  "uid": ["CHE-123.456.789"],
  "iban": ["CH93 0076 2011 6238 5295 7"],
  "email": [],
  "telefon": []
}
```

| Funktion | Ort | Zweck |
|---|---|---|
| `extract_ibans_from_text()` | `iban_utils.py` → `post_consume.py` | Kandidaten per Regex, nur gültige IBANs (Modulo 97, Länderlänge) |
| `validate_iban()` / `is_valid_iban_compact()` | `iban_utils.py` | Backend + Pipeline |
| `_normalize_identifikatoren()` | `correspondent_manager_app.py` | Speichern; IBAN-Fehler → HTTP 400 |

Tests: `tests/test_iban_utils.py`

### 7.4 API (Auszug)

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/api/correspondents/batch-platzhalter` | Body: `{ "names": ["Gesundheit", …], "platzhalter": true }` |
| POST | `/api/pipeline/trigger/{doc_id}` | Volle `post_consume`-Pipeline nachträglich (async, Subprozess) |
| GET | `/api/pipeline/trigger-status/{doc_id}` | Job-Status für UI-Polling |

Implementierung: `post_consume_runner.py` (Subprozess — `main()` nutzt `sys.exit`).

---

## 8. Legacy QR-Split — Entwickler

### 8.1 Unterschied

| Modul | QR-Typ | Wann |
|---|---|---|
| `pre_consume_qr.py` | Swiss QR-Bill `SPC` | Neuer Scan, Zahlungsdaten |
| Paperless Barcodes | PATCHT / ASN | Neuer Scan, Trennseiten |
| `legacy_split_by_qr.py` | `060102_Kategorie_Person` | Nachträglich, NAS-Altbestand |

Port von `tsa_barcode_split_function.sh`: **Ghostscript** (nicht Poppler allein) + pyzbar/zbar + Seiten-Split.

### 8.2 API (async)

`POST /api/legacy-split/trigger/{doc_id}` → sofort `{async: true}`; UI pollt:

`GET /api/legacy-split/trigger-status/{doc_id}?dry_run=true`

Body (JSON):

```json
{
  "dry_run": true,
  "sync": false,
  "regex": "^[0-9]{6}_[^\\s]+$",
  "consume_dir": "/mnt/paperless-data/consume"
}
```

- `dry_run: true` → Vorschau `{ok, pages, splits, scan_seconds, scan_meta}`
- `dry_run: false` → Split lokal, `shutil.move` nach `consume_dir`
- `sync: true` → blockierend (curl/Debug), kein Polling

### 8.3 Architektur (BE 2.51)

```
POST trigger → BackgroundTask → _LEGACY_SPLIT_EXECUTOR
  → PDF API → /tmp/legacy-qr-split/{id}/source.pdf
  → subprocess: legacy_qr_scan_worker.py (Hauptthread, pyzbar-sicher)
  → find_split_markers (ghostscript @ 150 dpi, early exit ab 2 Markern)
  → split_pdf_at_markers in /tmp/.../parts/ → move nach consume/
```

**Wichtig:** `LEGACY_SPLIT_QR_REGEX` in `.env` **mit Quotes** — sonst `[^\s]` → `[^s]` und 0 Treffer (Vollscan alle DPI).

`normalize_legacy_qr_regex()` in `legacy_split_by_qr.py` fängt kaputte Werte ab.

### 8.4 Abhängigkeiten & CLI

```bash
sudo ./scripts/ensure-legacy-qr-deps.sh   # poppler, ghostscript, zbar, venv
/opt/paperless-scripts/venv/bin/python3 legacy_qr_split_test.py /pfad/scan.pdf --verbose-pages
```

| Datei | Rolle |
|---|---|
| `legacy_split_by_qr.py` | Scan, Marker, Split-Logik |
| `legacy_qr_scan_worker.py` | Subprocess-Worker für UI |
| `scripts/legacy_qr_split_test.py` | CT121-Diagnose ohne Paperless |

Env: `PAPERLESS_CONSUME_DIR`, `LEGACY_SPLIT_QR_REGEX`, `LEGACY_SPLIT_TMP`

---

## 9. Wichtige `.env`-Variablen

| Variable | Zweck |
|---|---|
| `PAPERLESS_TOKEN` / `PAPERLESS_API_TOKEN` | Backend → Paperless API |
| `PAPERLESS_URL` | Kanonische URL (Domain) |
| `PAPERLESS_INTERNAL_URL` | Container-zu-Container, Session-Fallback |
| `PAPER_MANAGER_TOKEN` | Optional: API ohne Browser-Session |
| `PAPERLESS_CONSUME_DIR` | Legacy QR-Split Ziel |
| `LEGACY_SPLIT_QR_REGEX` | QR-Metadaten-Regex — **in .env mit Quotes:** `'^[0-9]{6}_[^\s]+$'` |
| `LEGACY_SPLIT_TMP` | Temp für PDF/Scan (Default `/tmp/legacy-qr-split`) |
| `BRILLENPASS_DEDUP_DAYS` | Perioden-Dedup (Default 21) |
| `BRILLENPAESSE_JSON` | Gespeicherte Versionen |
| `PENDING_BRILLENPASS_JSONL` | Review-Queue |

Vollständig: `.env.example`

---

## 10. Tests & lokale Entwicklung

```bash
cd paperless-ngx-classifier
python -m pytest tests/test_brillenpass_parsers.py tests/test_htr_sanitize.py -q
```

Backend lokal (ohne Paperless):

```bash
export PAPERLESS_TOKEN=...
export CORRESPONDENTS_JSON=training/correspondents.example.json
uvicorn correspondent_manager_app:app --host 0.0.0.0 --port 8100
```

UI: `paper_manager_ui.html` wird vom FastAPI-Root ausgeliefert.

---

## 10.1 Dependency-Audit (CVE)

Auf CT 121 (venv unter `/opt/paperless-scripts/venv`):

```bash
cd /opt/paperless-ngx-classifier
chmod +x scripts/dependency-audit.sh
./scripts/dependency-audit.sh
# oder explizit: ./scripts/dependency-audit.sh /opt/paperless-scripts/venv
```

Manuell ohne Skript:

```bash
/opt/paperless-scripts/venv/bin/python3 -m pip install pip-audit
/opt/paperless-scripts/venv/bin/pip-audit -r requirements-corr-manager.txt
```

Empfehlung: monatlich + vor Paperless/corr.manager-Deploy. Advisories: [GitHub Dependabot](https://github.com/advisories), [OWASP Dependency-Track](https://owasp.org/www-project-dependency-track/).

---

## 11. Deploy-Checkliste

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/deploy-to-ct121.sh
grep -m1 POST_CONSUME_VERSION /opt/paperless-scripts/post_consume.py
grep -m1 UI_VERSION /opt/paperless-scripts/correspondent_manager_app.py
systemctl restart correspondent-manager   # falls systemd-Unit
```

Sidebar: `UI v3.09 | be v2.56 | pipe v12.72`

---

## 12. Private Doku (Git `/doku`)

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
