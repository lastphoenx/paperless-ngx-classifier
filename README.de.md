# paperless-ngx-classifier

**KI-gestützte Dokumentenklassifizierung für Paperless-NGX — lokale LLMs, Vision-OCR, selbstlernend, ohne Cloud.**

> Dokument einscannen. Weggehen. Zurückkommen zu einem vollständig klassifizierten, getaggten und abgelegten Dokument — mit richtigem Korrespondenten, Speicherpfad, Dokumenttyp und ausgefüllten benutzerdefinierten Feldern. Keine Cloud. Kein Abo. Keine Daten ausserhalb deiner Infrastruktur.

[🇬🇧 English version](README.md)

---

## Warum gibt es das?

[Paperless-NGX](https://docs.paperless-ngx.com/) ist ein ausgezeichnetes Dokumentenmanagementsystem — die eingebaute Klassifizierung ist aber auf OCR-Textabgleich und einfache Regeln beschränkt. Sie kann nicht:

- Dokumente **als Bild analysieren** (Logoerkennung, Layout, Handschrift)
- **Handschriftliche Notizen** erkennen (z. B. ein Zahlungsdatum in der Ecke)
- Dokumente anhand von **Fahrzeugkennzeichen** ohne manuelle Regeln zuordnen
- Aus **Korrekturen lernen** und sich verbessern
- **Schweizer QR-Rechnung**-Daten parsen und benutzerdefinierte Felder automatisch befüllen

Dieses Projekt fügt Paperless-NGX eine intelligente Pre/Post-Consume-Pipeline hinzu, die all das löst — mit **lokalen LLMs via Ollama**, sodass deine Dokumente deine Infrastruktur nie verlassen.

---

## Wie funktioniert es?

```
Scanner
  ↓
pre_consume.sh        — OCR-Optimierung (ocrmypdf) + Barcode-Splitting
pre_consume_qr.py     — Schweizer QR-Rechnung parsen (IBAN, Betrag, Referenz, Fälligkeitsdatum)
  ↓
post_consume.py       — Haupt-Pipeline (läuft nach jedem erfolgreichen Scan)
  ├─ Vision LLM       — Analysiert Dokument als Bild: Absender, Datum, Betrag,
  │                     Kennzeichen, handschriftliche Notizen ("bez. 6.2.26" → bezahlt)
  ├─ RAG              — Embeddings (bge-m3) gleichen Dokument mit bekannten Ordnern ab
  ├─ LLM              — Klassifiziert Dokumenttyp, Tags, Speicherpfad
  ├─ Sanitiser        — Validiert gegen Manifest, Ausschluss-Keywords
  ├─ Deterministisch  — Kennzeichen aus family.json umgehen das LLM vollständig
  │   Routing           (schneller + zuverlässiger für bekannte Muster)
  └─ Paperless API    — Setzt Korrespondent, Tags, Pfad, benutzerdefinierte Felder
  ↓
paper.manager         — Browser-UI zum Reviewen unsicherer Dokumente,
(Port 8100)             Korrespondenten, Dokumenttypen, Tags,
                        Speicherpfade und Haushaltskonfiguration verwalten
```

### Der Lernkreislauf

Jede Korrektur in paper.manager fliesst zurück ins System:

- Bestätigte Korrespondenten → werden mit Match-Strings in `correspondents.json` aufgenommen
- Umklassifizierte Dokumente → erlaubte Tags im Manifest werden aktualisiert
- Bekannte Absender → kommen nie wieder in die Review-Warteschlange
- Deterministisches Routing → wächst mit der Zeit, reduziert LLM-Aufrufe

Mit der Zeit: **mehr deterministisch, weniger LLM, schneller, genauer.**

---

## Kernfunktionen

### Vision-First-Analyse

Jedes Dokument wird von einem multimodalen LLM (`qwen2.5vl`) als **Bild** analysiert, nicht nur als OCR-Text. So werden Logos, Layouts, Stempel und Handschriften erkannt, die OCR übersieht.

### Handschrifterkennung

Jemand schreibt `bez. 6.2.26` in die obere rechte Ecke bezahlter Rechnungen. Das Vision-Modell liest es, `parse_handschrift_bezahlt()` extrahiert das Datum, und Paperless erhält:
- Benutzerdefiniertes Feld `Status` → `Bezahlt`
- Benutzerdefiniertes Feld `Bezahlt am` → `2026-02-06`

Das ermöglicht einen starken Anwendungsfall: In Paperless nach `Bezahlt am = 2026-02-06` suchen und mit dem E-Banking-Auszug dieses Tages abgleichen.

### Schweizer QR-Rechnung parsen

Automatisch extrahiert und befüllt:
- Betrag (`CHF`)
- Rechnungsnummer
- Kundennummer
- QR-Referenz (27-stellig)
- Fälligkeitsdatum

### Deterministisches Routing

Fahrzeugkennzeichen in `family.json` konfigurieren. Wenn das Vision-Modell `XX 000001` auf einem Dokument erkennt, geht es direkt zu `Person/Auto` — kein LLM-Aufruf nötig.

### Benutzerdefinierte Felder — automatisch befüllt

| Feld | Typ | Quelle |
|---|---|---|
| CHF | Geldbetrag | QR-Rechnung |
| Rechnungsnummer | Text | QR-Rechnung / Vision |
| Kundennummer | Text | Vision |
| QR-Referenz | Text | QR-Rechnung |
| Fällig am | Datum | QR-Rechnung |
| Status | Auswahl | Automatisch (Offen/Bezahlt) |
| Policennummer | Text | Vision |
| Kennzeichen | Auswahl | Vision + family.json |
| Bezahlt am | Datum | Handschrift `bez.` |
| Eingescannt am | Datum | Immer = heute |

### paper.manager UI

Eine Single-Page-Browser-UI (kein Framework, kein Build-Schritt) für:
- **Korrespondenten-Review** — unbekannte Absender bestätigen, ablehnen oder zusammenführen
- **Dokument-Review** — Dokument-Vorschaubild, KI-Felder (Titel, Korrespondent, Ordner, Typ, Datum, farbige Confidence, Review-Grund, **LLM-Begründung**), Tags als Chips, Custom Fields, Korrekturformular (Ordner, Korrespondent, Typ, Tags)
- **Dokumenttypen** — Synonyme und Ausschluss-Keywords verwalten
- **Tags** — Ausschluss-Keywords pro Tag verwalten
- **Speicherpfade** — Ordner mit erlaubten Tags und Dokumenttypen konfigurieren
- **Familie** — Personen, Fahrzeuge, Haushaltsname (keine Hardcodierung im Code)
- **Versionsanzeige** — zeigt aktive Versionen aller Komponenten in der Seitenleiste

---

## Vorher / Nachher

| | Ohne diese Pipeline | Mit dieser Pipeline |
|---|---|---|
| Absendererkennung | Nur OCR-Textabgleich | Vision + Fuzzy-Matching + Lernen |
| Dokumenttyp | Manuell oder einfache Regeln | LLM + Synonymauflösung + Ausschlüsse |
| Handschrift | Nicht möglich | Erkannt und geparst |
| Kennzeichen-Routing | Manuelle Regel pro Kennzeichen | In UI konfiguriert, deterministisch |
| Benutzerdefinierte Felder | Manuell | Automatisch (QR-Rechnung + Vision) |
| Unbekannte Absender | Stille Fehler | Review-Warteschlange mit Vorschlägen |
| Korrekturen | Verloren | Fliessen in nächste Klassifizierung zurück |
| Datenschutz | Abhängig von OCR/KI-Dienst | 100 % lokal, keine Cloud |

---

## Voraussetzungen

| Komponente | Details |
|---|---|
| Paperless-NGX | v2.x, Docker |
| Ollama | Separater Server empfohlen (GPU) |
| Python | 3.11+ auf dem Paperless-Host |
| Betriebssystem | Debian 12 / Ubuntu 24.04 (andere möglich) |

### Empfohlene Ollama-Modelle

| Modell | Zweck | Min. VRAM/RAM |
|---|---|---|
| `qwen2.5vl:7b` | Vision — Dokument-Bild analysieren | 16 GB |
| `llama3.3:70b` | LLM — Klassifizierung, Routing | 64 GB RAM (CPU-Inferenz möglich) |
| `bge-m3` | Embeddings (optional, verbessert RAG) | — |

> Getestet auf GMKtec EVO mit AMD Ryzen AI Max+ 395, 128 GB RAM. Langsamere Hardware funktioniert ebenfalls — die Verarbeitungszeit steigt, die Qualität bleibt gleich. Durch das Lernen werden mit der Zeit weniger LLM-Aufrufe benötigt.

---

## Schnellstart

```bash
git clone https://github.com/lastphoenx/paperless-ngx-classifier.git /tmp/classifier

# Scripts deployen
cp /tmp/classifier/post_consume.py        /opt/paperless-scripts/
cp /tmp/classifier/pre_consume.sh         /opt/paperless-scripts/
cp /tmp/classifier/pre_consume_qr.py      /opt/paperless-scripts/
cp /tmp/classifier/correspondent_manager_app.py /opt/paperless-scripts/
cp /tmp/classifier/paper_manager_ui.html  /opt/paperless-scripts/

# Trainingsdateien initialisieren
mkdir -p /opt/paperless-scripts/training
cp /tmp/classifier/training/family.example.json         /opt/paperless-scripts/training/family.json
cp /tmp/classifier/training/document_types.example.json /opt/paperless-scripts/training/document_types.json
cp /tmp/classifier/training/manifest.example.json       /opt/paperless-scripts/training/manifest.json
cp /tmp/classifier/training/correspondents.example.json /opt/paperless-scripts/training/correspondents.json
cp /tmp/classifier/training/tags.example.json           /opt/paperless-scripts/training/tags.json

# Konfigurieren
cp /tmp/classifier/.env.example /opt/paperless/.env
nano /opt/paperless/.env
```

**Vollständige Installationsanleitung** → [`INSTALL.md`](INSTALL.md)  
**Benutzerhandbuch (paper.manager)** → [`docs/Benutzerhandbuch_paper_manager.md`](docs/Benutzerhandbuch_paper_manager.md)

> **`docker-compose.yml`** in diesem Repo ist eine **Vorlage** für einen vollständigen Paperless-NGX Docker-Stack (DB, Broker, Webserver). Nur verwenden, wenn Paperless noch nicht installiert ist. Alle Pfade, Passwörter und Volumes vor der Nutzung anpassen — alle Variablen in `.env.example`.

---

## Konfigurationsdateien (`training/`)

| Datei | Funktion |
|---|---|
| `family.json` | Haushalt: Personen und Fahrzeuge (Basis für Ordnerstruktur + deterministisches Routing) |
| `correspondents.json` | Bekannte Absender mit Fuzzy-Match-Regeln und Extraktionsmustern |
| `document_types.json` | Dokumenttypen mit Synonymen und Ausschluss-Keywords |
| `manifest.json` | Speicherordner-Struktur mit erlaubten Tags und Dokumenttypen |
| `tags.json` | Tags mit Ausschluss-Keywords |
| `pending_mode.txt` | Pipeline-Modus: `always` / `uncertain` / `never` |

> Diese Dateien sind **nicht** im Repo enthalten (sie enthalten persönliche Daten). Für jede Datei sind Beispieldateien mit Platzhalterwerten vorhanden.

### `.env` — wichtige Variablen

| Variable | Standard | Funktion |
|---|---|---|
| `CONFIDENCE_IGNORE_TAG_PATTERNS` | `^\d{4}$,^\d{1,2}\.\d{4}$` | Regex-Muster für Tags, die die Confidence **nicht** senken (Jahreszahlen, Monat.Jahr). Kommagetrennt. Leer = alles deaktiviert. |
| `CF_BEZAHLT_AM_ID` | — | Paperless Custom-Field-ID für «Bezahlt am» |
| `CF_GESCANNT_AM_ID` | — | Paperless Custom-Field-ID für «Eingescannt am» |

Alle Variablen mit Beschreibungen siehe `.env.example`.

---

## Komponenten

| Datei | Funktion |
|---|---|
| `post_consume.py` | Haupt-Pipeline: wird von Paperless nach jedem Scan aufgerufen |
| `pre_consume.sh` | Vorverarbeitung: ruft `pre_consume_qr.py` auf |
| `pre_consume_qr.py` | QR-Bill-Parser: extrahiert Betrag, IBAN, Referenz aus Schweizer QR-Rechnungen |
| `correspondent_manager_app.py` | FastAPI-Backend für paper.manager Review-UI |
| `paper_manager_ui.html` | Browser-UI zum Reviewen, Trainieren, Konfigurieren |
| `docker-compose.yml` | Paperless-NGX Stack (Vorlage — Pfade und Passwörter anpassen) |
| `.env.example` | Alle Konfigurationsvariablen mit Erklärungen |
| `training/` | Beispiel-Konfigurationsdateien für Korrespondenten, Dokumenttypen, Manifest etc. |

---

## Screenshots

| | |
|---|---|
| ![Korrespondenten](docs/screenshots/Korrespondenten.PNG) | ![Dokumenttypen](docs/screenshots/Doktypen.PNG) |
| ![Tags](docs/screenshots/Tags.PNG) | ![Speicherpfade](docs/screenshots/Speicherpfade.PNG) |

---

## paper.manager UI

Verfügbar unter `http://SERVER_IP:8100` nach der Installation.

| Tab | Funktion |
|---|---|
| Home | Systemübersicht, Feature-Zusammenfassung, Komponentenversionen |
| Correspondent Review | Unbekannte Absender bestätigen / ablehnen / zusammenführen |
| Correspondents | Bekannte Absender bearbeiten |
| Document Review | Vorschaubild + KI-Felder + farbige Confidence + LLM-Begründung + Korrekturformular |
| Document Types | Synonyme + Ausschluss-Keywords |
| Tags | Ausschluss-Keywords pro Tag |
| Speicherpfade | Ordnerkonfiguration |
| Familie | Haushaltsname, Personen, Fahrzeuge |

---

## Sicherheitshinweis

- **Niemals** `.env` committen — enthält API-Tokens, DB-Passwort und Secret Key
- `training/*.json` / `training/*.jsonl` enthalten persönliche Daten → nicht committed
- `.gitignore` in diesem Repo schützt diese Dateien bereits
- paper.manager ist durch Paperless-Session-Cookie geschützt; für den Produktionseinsatz Authentik oder nginx Basic Auth vorschalten

---

## Lizenz

MIT

---

## ⚠️ Disclaimer / Haftungsausschluss — KI-generierter Code

> **IN ENTWICKLUNG** — Neue Funktionen werden hinzugefügt, bestehende werden getestet und verbessert. Für den produktiven Einsatz bitte eigene Tests durchführen und regelmässig auf Updates prüfen.

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

*Nicht wundern über gelegentlich lustige Commits, viele Emojis und andere KI-typische Stilelemente. Der Code funktioniert, wurde getestet und läuft produktiv — der Schreibstil ist aber definitiv… enthusiastisch.*

---

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
