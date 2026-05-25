# paperless-ngx-classifier

**AI-powered document classification pipeline for Paperless-NGX — local LLMs, Vision OCR, auto-learning, zero cloud.**

> Scan a document. Walk away. Come back to a fully classified, tagged, and filed document — with the right correspondent, storage path, document type, and custom fields filled in. No cloud. No subscription. No data leaving your home.

[🇩🇪 Deutsche Version](README.de.md)

---

## Why does this exist?

[Paperless-NGX](https://docs.paperless-ngx.com/) is an excellent document management system — but its built-in classification is limited to OCR text matching and simple rules. It cannot:

- Analyse the document **as an image** (logo recognition, layout, handwriting)
- Detect **handwritten notes** (e.g. a payment date scribbled in the corner)
- Route documents based on **vehicle licence plates** without manual rules
- Learn from **your corrections** and improve over time
- Parse **Swiss QR-Bill** data and populate custom fields automatically

This project adds an intelligent pre/post-consume pipeline to Paperless-NGX that addresses all of these — using **local LLMs via Ollama**, so your documents never leave your infrastructure.

---

## How it works

```
Scanner
  ↓
pre_consume.sh        — OCR optimisation (ocrmypdf) + barcode splitting
pre_consume_qr.py     — Swiss QR-Bill parsing (IBAN, amount, reference, due date)
  ↓
post_consume.py       — Main pipeline (runs after every successful scan)
  ├─ Vision LLM       — Analyses document as image: sender, date, amount,
  │                     licence plate, handwritten notes ("bez. 6.2.26" → paid)
  ├─ RAG              — Embeddings (bge-m3) match document to known folders
  ├─ LLM              — Classifies document type, tags, storage path
  ├─ Sanitiser        — Validates against manifest, exclusion keywords
  ├─ Deterministic    — Licence plates from family.json bypass LLM entirely
  │   routing           (faster + more reliable for known patterns)
  └─ Paperless API    — Patches correspondent, tags, path, custom fields
  ↓
paper.manager         — Browser UI for reviewing uncertain documents,
(port 8100)             managing correspondents, document types, tags,
                        storage paths, and household configuration
```

### The learning loop

Every correction you make in paper.manager feeds back into the system:

- Approved correspondents → added to `correspondents.json` with match strings
- Reclassified documents → allowed tags updated in the manifest
- Known senders → never go into the review queue again
- Deterministic routing → grows over time, reducing LLM calls

Over time: **more deterministic, less LLM, faster, more accurate.**

---

## Key features

### Vision-first analysis
Every document is analysed as an **image** by a multimodal LLM (`qwen2.5vl`), not just as OCR text. This catches logos, layouts, stamps, and handwriting that OCR misses.

### Handwriting recognition
Someone writes `bez. 6.2.26` in the top-right corner of paid invoices. The vision model reads it, `parse_handschrift_bezahlt()` extracts the date, and Paperless gets:
- Custom field `Status` → `Bezahlt`
- Custom field `Bezahlt am` → `2026-02-06`

This enables a killer use case: search Paperless for `Bezahlt am = 2026-02-06` and cross-reference with your e-banking statement for that day.

### Swiss QR-Bill parsing
Automatically extracts and populates:
- Amount (`CHF`)
- Invoice number
- Customer number
- QR reference (27-digit)
- Due date (`Fällig am`)

### Deterministic routing
Configure vehicle licence plates in `family.json`. When the vision model spots `XX 000001` on a document, it goes directly to `Person/Auto` — no LLM call needed.

### Custom fields — automatically filled

| Field | Type | Source |
|---|---|---|
| CHF | Monetary | QR-Bill |
| Invoice number | Text | QR-Bill / Vision |
| Customer number | Text | Vision |
| QR reference | Text | QR-Bill |
| Due date | Date | QR-Bill |
| Status | Select | Auto (Open/Paid) |
| Policy number | Text | Vision |
| Licence plate | Select | Vision + family.json |
| Paid on | Date | Handwriting `bez.` |
| Scanned on | Date | Always = today |

### paper.manager UI
A single-page browser UI (no framework, no build step) for:
- **Correspondent review** — approve, reject, or merge unknown senders
- **Document review** — confirm or correct uncertain classifications
- **Document types** — manage synonyms and exclusion keywords
- **Tags** — manage exclusion keywords per tag
- **Storage paths** — configure folders with allowed tags and document types
- **Family config** — persons, vehicles, household name (no hardcoding in code)
- **Version display** — shows active versions of all components in the sidebar

---

## Before / After

| | Without this pipeline | With this pipeline |
|---|---|---|
| Sender detection | OCR text matching only | Vision + fuzzy matching + learning |
| Document type | Manual or simple rules | LLM + synonym resolution + exclusions |
| Handwriting | Not possible | Recognised and parsed |
| Licence plate routing | Manual rule per plate | Configured in UI, deterministic |
| Custom fields | Manual | Automatic (QR-Bill + Vision) |
| Unknown senders | Silent failure | Review queue with suggested values |
| Corrections | Lost | Feed back into next classification |
| Data privacy | Depends on OCR/AI service | 100% local, zero cloud |

---

## Requirements

| Component | Details |
|---|---|
| Paperless-NGX | v2.x, Docker |
| Ollama | Separate server recommended (GPU) |
| Python | 3.11+ on Paperless host |
| OS | Debian 12 / Ubuntu 24.04 (others possible) |

### Recommended Ollama models

| Model | Purpose | Min. RAM |
|---|---|---|
| `qwen2.5vl:7b` | Vision — image analysis | 16 GB |
| `llama3.3:70b` | LLM — classification | 64 GB (CPU possible) |
| `bge-m3` | Embeddings (optional, improves RAG) | — |

> Tested on GMKtec EVO with AMD Ryzen AI Max+ 395, 128 GB RAM. Slower hardware works too — processing time increases but quality is the same. With learning, fewer LLM calls are needed over time.

---

## Quick start

```bash
git clone https://github.com/lastphoenx/paperless-ngx-classifier.git /tmp/classifier

# Deploy scripts
cp /tmp/classifier/post_consume.py        /opt/paperless-scripts/
cp /tmp/classifier/pre_consume.sh         /opt/paperless-scripts/
cp /tmp/classifier/pre_consume_qr.py      /opt/paperless-scripts/
cp /tmp/classifier/correspondent_manager_app.py /opt/paperless-scripts/
cp /tmp/classifier/paper_manager_ui.html  /opt/paperless-scripts/

# Initialise training files
mkdir -p /opt/paperless-scripts/training
cp /tmp/classifier/training/family.example.json         /opt/paperless-scripts/training/family.json
cp /tmp/classifier/training/document_types.example.json /opt/paperless-scripts/training/document_types.json
cp /tmp/classifier/training/manifest.example.json       /opt/paperless-scripts/training/manifest.json
cp /tmp/classifier/training/correspondents.example.json /opt/paperless-scripts/training/correspondents.json
cp /tmp/classifier/training/tags.example.json           /opt/paperless-scripts/training/tags.json

# Configure
cp /tmp/classifier/.env.example /opt/paperless/.env
nano /opt/paperless/.env
```

**Full installation guide** → [`INSTALL.md`](INSTALL.md)  
**User handbook (paper.manager)** → [`docs/Benutzerhandbuch_paper_manager.md`](docs/Benutzerhandbuch_paper_manager.md)

> **`docker-compose.yml`** included in this repo is a **template** for a full Paperless-NGX Docker stack (DB, broker, webserver). Use it only if Paperless is not yet installed. Adapt all paths, passwords, and volumes before use — see `.env.example` for all variables.

---

## Configuration files (`training/`)

| File | Purpose |
|---|---|
| `family.json` | Household: persons and vehicles (basis for folder structure + deterministic routing) |
| `correspondents.json` | Known senders with fuzzy match rules and extraction patterns |
| `document_types.json` | Document types with synonyms and exclusion keywords |
| `manifest.json` | Storage folder structure with allowed tags and document types |
| `tags.json` | Tags with exclusion keywords |
| `pending_mode.txt` | Pipeline mode: `always` / `uncertain` / `never` |

> These files are **not** included in the repo (they contain personal data). Example files with placeholder values are provided for each.

---

## Screenshots

| | |
|---|---|
| ![Correspondents](docs/screenshots/Korrespondenten.PNG) | ![Document Types](docs/screenshots/Doktypen.PNG) |
| ![Tags](docs/screenshots/Tags.PNG) | ![Storage Paths](docs/screenshots/Speicherpfade.PNG) |

---

## paper.manager UI

Available at `http://SERVER_IP:8100` after installation.

| Tab | Purpose |
|---|---|
| Home | System overview, feature summary, component versions |
| Correspondent Review | Approve / reject / merge unknown senders |
| Correspondents | Edit known senders |
| Document Review | Confirm or correct uncertain classifications |
| Document Types | Synonyms + exclusion keywords |
| Tags | Exclusion keywords per tag |
| Speicherpfade | Folder configuration |
| Familie | Household name, persons, vehicles |

---

## Security note

- **Never** commit `.env` — it contains API tokens, DB password, and secret key
- `training/*.json` / `training/*.jsonl` contain personal data → not committed
- `.gitignore` in this repo already protects these files
- paper.manager is protected by Paperless session cookie; put Authentik or nginx basic auth in front for production use

---

## Licence

MIT

---

## ⚠️ Disclaimer / Haftungsausschluss — AI-Generated Code

> **WORK IN PROGRESS** — New features are being added, existing ones are being tested and improved. For productive use, please run your own tests and check for updates regularly.

### 🇬🇧 English: AI-Generated Code Notice

This repository was created using multiple AI systems. The code has been **entirely generated by AI** — not a single line was manually written by a human. All development took place in **Microsoft Visual Studio Code (VS Code)** using **GitHub Copilot** and various AI models.

**My role as developer:**
- ✅ Designed logic and architecture
- ✅ Guided and optimised prompts
- ✅ Reviewed code and reported errors
- ✅ Conducted tests and reported bugs
- ❌ Did not write a single line of code myself

**This also applies to:**
- All commits (commit messages generated by AI)
- Complete documentation (including this README)
- Configuration files and scripts

*Don’t be surprised by occasionally funny commits, lots of emojis, and other AI-typical style elements. The code works, has been tested, and runs in production — but the writing style is definitely… enthusiastic.*

---

### 🇩🇪 Deutsch: Hinweis zu KI-generiertem Code

Dieses Repository wurde mit mehreren KI-Systemen erstellt. Der Code wurde bisher **vollständig von KI erzeugt** — keine Zeile wurde manuell von einem Menschen geschrieben. Die gesamte Entwicklung erfolgte in **Microsoft Visual Studio Code (VS Code)** mit **GitHub Copilot** und verschiedenen KI-Modellen.

**Meine Rolle als Entwickler:**
- ✅ Logik und Architektur entworfen
- ✅ Prompts gesteuert und optimiert
- ✅ Code reviewed und auf Fehler hingewiesen
- ✅ Tests durchgeführt und Bugs gemeldet
- ❌ Keine einzige Zeile Code selbst geschrieben

**Das gilt auch für:**
- Alle Commits (Commit-Messages von KI generiert)
- Gesamte Dokumentation (inkl. dieses README)
- Konfigurationsdateien und Scripts
