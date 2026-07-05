# paper.manager — Benutzerhandbuch

**Version 2.47 | Juli 2026** (Pipeline `12.44`, Backend `2.35`)

> Entwickler-Details: [`DEVELOPER.md`](DEVELOPER.md) · Legacy-Import: [`LEGACY_IMPORT.md`](LEGACY_IMPORT.md)

---

## 1. Einführung

paper.manager ist die Review- und Verwaltungsoberfläche für die automatische Dokumentklassifizierung mit Paperless-NGX. Nach jedem Scan analysiert ein lokales KI-System (Vision + LLM) das Dokument vollständig als Bild — erkennt Absender, Datum, Betrag, Kennzeichen und handschriftliche Notizen.

> 💡 **Tipp:** Bezahlte Rechnungen mit `bez. 6.2.26` oben rechts markieren — das System setzt automatisch `Status=Bezahlt` und `Bezahlt am=06.02.2026`.

Klick auf **«paper.manager»** im Logo öffnet die Landing Page mit vollständiger Systemübersicht.

### Zugangswege

| Zugang | URL | Auth |
|---|---|---|
| Via Domain (empfohlen) | https://paperless.example.com/corr-manager/ | Authentik SSO |
| Via interne IP | `http://<IP-des-Servers>:8100` | Paperless-Login auf **derselben IP** (`:8000`) — Session-Cookie wird host-aware geprüft |

> **Auth (ab BE 2.35):** API-Calls prüfen die Paperless-Session gegen die **gleiche Basis-URL wie der Browser-Zugriff** — per IP also `http://<IP>:8000`, per Domain die externe URL. Zuvor konnte `PAPERLESS_URL` (Domain) und IP-Zugriff kollidieren → `401 Nicht authentifiziert` trotz Login.

> **PDF-Vorschau per IP:** Thumbnail und PDF im Dokument-Review laufen über
> `/api/proxy/document/{id}/thumb/` bzw. `/preview/` — das Backend holt die Datei mit
> `PAPERLESS_TOKEN` aus der Paperless-API. Ohne Proxy scheitert die Vorschau per IP oft
> (kein Authentik-/Session-Cookie für direkte Paperless-URLs).

### Versionsanzeige

Direkt unter dem Logo zeigt die Sidebar die aktuellen Versionen:
```
UI v2.47 | be v2.35 | pipe v12.44
```
Stimmt die Version nicht → Ctrl+Shift+R oder Service-Restart. Regeln zum Hochzählen: `docs/VERSIONING.md`.

### Navigation

| Menüpunkt | Hash | Funktion |
|---|---|---|
| Logo-Klick | `#home` | Landing Page — Systemübersicht |
| ! Korrespondenten Review | `#pending` | Neue Absender freigeben |
| # Korrespondenten | `#correspondents` | Absender verwalten |
| D Dokument-Review | `#docreview` | Unsichere Dokumente prüfen, Vorschau + LLM-Begründung |
| T Dokumenttypen | `#doctypes` | Synonyme + Ausschluss-Keywords |
| ~ Tags | `#tags` | Tags + Ausschluss-Keywords |
| M Manifest | `#manifest` | Ordner konfigurieren |
| 👪 Familie | `#family` | Haushalt, Personen, Referenzen (Kennzeichen), Beziehungen |
| ✂ Legacy QR-Split | `#legacy-split` | Mehrseiten-Scans nachträglich an QR splitten → `consume/` |
| 👓 Brillenpass | `#brillenpass` | Optiker-Dokumente parsen, Review, versionierter Pass pro Person |

Alle Tabs haben ein **Suchfeld** für live Filterung (wo vorhanden).

---

## 2. QS-Modus Toggle

| Status | Bedeutung |
|---|---|
| AUS (grau) — Nur unsichere | Nur Dokumente mit niedriger KI-Konfidenz in der Queue |
| EIN (grün) — QS-Modus aktiv | ALLE gescannten Dokumente zur Prüfung |

Status wird serverseitig gespeichert — bleibt nach Refresh erhalten.

---

## 3. Handschrift-Erkennung

**Stufe 1 — Vision:** `qwen2.5vl:7b` analysiert das Dokument als Bild, sucht Handschrift oben rechts.
**Stufe 2 — Regex:** `parse_handschrift_bezahlt()` extrahiert das Datum.

| Handschrift | Ergebnis |
|---|---|
| `bez. 6.2.26` | ✓ Status=Bezahlt, Bezahlt am=06.02.2026 |
| `bez 26.3.2026` | ✓ Status=Bezahlt, Bezahlt am=26.03.2026 |
| `BEZ 6.2.26` | ✓ Status=Bezahlt |
| `bezahlt 6.2.26` | ✓ Status=Bezahlt |
| `bz. 6.2.26` | ✓ Status=Bezahlt |
| `EZ 26.3.26` | ✗ **Nicht erkannt** — EZ = Einzahlung, kein Bezahlt-Vermerk |
| (keine Notiz) | → Status=Offen (bei Rechnungen automatisch) |

> ⚠️ `EZ` wird bewusst nicht erkannt — es steht für «Einzahlung» (Bankbuchung).

### Gesetzte Custom Fields bei Bezahlt-Vermerk
- **Status** → `Bezahlt`
- **Bezahlt am** → Datum aus Handschrift (für Zahllauf-Abgleich im E-Banking)
- **Gescannt am** → immer = heutiges Datum (bei jedem Dokument)

---

## 4. Korrespondenten Review

Unbekannte Absender → Warteschlange (roter Badge).

### Felder beim Freigeben

| Feld | Bedeutung |
|---|---|
| Kanonischer Name | Offizieller Absender-Name |
| **Kürzel** | 2–6 Zeichen, eindeutig (z. B. `UBS`) — erscheint im Titel-Suffix und als Badge |
| Standard-Dokumenttyp | Typischer Dokumenttyp dieses Absenders |
| Varianten | Alternative Schreibweisen (Enter / × ) |
| Match-Strings | Suchbegriffe für Paperless-Matching |
| Typische Ordner | Format: Hauptordner/Unterordner |
| Notiz | Interne Bemerkung |

### Aktionen
- **Freigeben** → Korrespondent in Paperless + correspondents.json, neue Ordner als PENDING im Manifest
- **Ablehnen** → Eintrag verworfen; betroffene Dokumente erhalten `pending_review` und erscheinen im **Dokument-Review**
- **⇔ Merge** → nur bei Fuzzy-Match (ähnlicher Name bereits in Map); Duplikate zusammenführen vor Freigabe

> ⚠️ Tags werden nicht auf Korrespondenten-Ebene gepflegt.

---

## 5. Korrespondenten verwalten

Edit-Button beim Eintrag. Felder: Standard-Dokumenttyp, Varianten, Match-Strings, Typische Ordner, Notiz.

### Brillenpass am Korrespondenten (Optiker)

Für Optiker, Augenärzte und ähnliche Absender, deren Dokumente Brillenwerte enthalten:

| Feld | Bedeutung |
|---|---|
| **Brillenpass aktiv** | Pipeline versucht automatisch Glaswerte zu extrahieren |
| **Optiker (Vendor)** | z. B. `fielmann`, `mcoptic`, `optik_meyer`, `augenarzt` — System erkennt **automatisch** ob Rechnung (A4) oder Brillenpass-Karte |
| **Typische Begriffe** | OCR-Hilfen für Erkennung (optional) |
| **Erweitert: Parser** | Nur bei Bedarf manuell einschränken (sonst leer lassen) |

Beispiel McOptic in `correspondents.json`:

```json
"brillenpass": {
  "aktiv": true,
  "vendor": "mcoptic",
  "typische_begriffe": ["McOptic", "Quittung", "SPH ZYL"]
}
```

**Merge:** ⇔ Mit anderem zusammenführen → alle Dokumente umgeschrieben, Duplikat gelöscht.

Suchfeld filtert nach Name, Varianten, Match-Strings, Ordnern, Notiz.

---

## 6. Dokument-Review

Dokumente mit einem der pending-Tags landen automatisch in der Review-Warteschlange:

| Tag | Farbe | Bedeutung |
|---|---|---|
| `pending_review` | gelb | KI unsicher, Datum verdächtig, Fallback-Ordner |
| `pending_qs` | grün | QS-Modus — alle Dokumente prüfen |
| `pending_new_correspondent` | rot | Unbekannter Absender — erscheint auch in Dokument-Review (Grund: «Korrespondent offen») |

### Panel-Aufbau (30/70)

**Links (ca. 30 %) — Vorschau**
- Grosses Thumbnail der ersten Seite (Proxy → Paperless-API)
- Klick öffnet das PDF in neuem Tab (Proxy → `/api/proxy/document/{id}/preview/`)
- Funktioniert auch bei Zugriff über interne IP ohne Authentik

**Rechts (ca. 70 %) — KI-Erkennung + Korrektur**

| Feld | Beschreibung |
|---|---|
| Titel | Vom LLM generierter Vorschlag |
| Korrespondent | Erkannter Absender — Dropdown nur **freigegebene** Korrespondenten (Map + Paperless-ID, ohne pending-NEU) |
| Ordner | Zugewiesener Speicherpfad |
| Dokumenttyp | Erkannter Dokumenttyp |
| Datum | Erkanntes Belegdatum |
| Confidence | Farbig: grün ≥90 %, gelb 70 %–89 %, rot <70 % |
| Review-Grund | Warum das Dokument in die Queue kam |
| LLM-Begründung | Erklärung des LLM zur Einschätzung |

Unter den KI-Feldern:
- **Tags als Chips** — alle gesetzten Tags angezeigt
- **Custom Fields** — nur gefüllte Felder, Status-ID wird als «Bezahlt»/«Offen» lesbar dargestellt

### Korrekturen (2×2-Grid)

| Feld | Funktion |
|---|---|
| Ordner | Anderen Speicherpfad wählen |
| Korrespondent | Anderen Absender wählen |
| Dokumenttyp | Anderen Typ wählen (NEU) |
| Tags | Tags wählen — bei **Neu klassifizieren** werden bestehende Tags **ersetzt** (nicht angehängt) |

### Aktionen

- **✓ Freigeben** — pending-Tags entfernen, Dokument freigeben
- **✎ Neu klassifizieren** — gewählte Korrekturen (Ordner, Korrespondent, Typ, Tags) anwenden und Manifest + Korrespondenten-Modell trainieren
- **✗ Ignorieren** — aus Queue entfernen ohne Änderungen

---

## 7. Dokumenttypen

### Feldprofil (Custom Fields pro Typ)

Im Edit-Dialog jedes Dokumenttyps: Tabelle **Extrahieren / Im Review / Pflicht** pro Custom Field.
- **Extrahieren** — Pipeline/OCR befüllt dieses Feld nur wenn angehakt
- **Im Review** — Feld im Dokument-Review-Formular sichtbar
- **Pflicht** — muss gesetzt sein vor Freigabe (Review-Hinweis)

Pipeline-Felder **Verarbeitung** und **Person** werden unabhängig vom Feldprofil gesetzt (siehe Abschnitt 12).

### Synonyme
Global einmalig (Unique-Constraint). Enter zum Hinzufügen, × oder Backspace zum Entfernen.
Direkt auf «Speichern» klicken ohne Enter übernimmt getippten Wert automatisch.

### Ausschluss-Keywords
Pro Typ definierbar — wenn Keyword im Dokument vorkommt, wird dieser Typ **nicht** zugewiesen.
Beispiel: `Servicerechnung` + Ausschluss `Strassenverkehrsamt` → Verkehrssteuern nie als Servicerechnung.
Nicht Unique-pflichtig (gleiches Keyword bei mehreren Typen erlaubt).

Suchfeld filtert nach Name, Beschreibung, Synonymen und Ausschluss-Keywords.

---

## 8. Tags

Cards mit Dokumentanzahl und Ausschluss-Keywords. Edit öffnet Bearbeitungsdialog.

**Umbenennen:** Neuen Namen → Speichern → direkt in Paperless.

**Ausschluss-Keywords:** Wenn Keyword im Dokument → Tag wird nicht gesetzt.
Beispiel: Tag `Service` + Ausschluss `Strassenverkehrsamt` → kein «Service»-Tag bei Verkehrssteuern.

---

## 9. Manifest

Erlaubte Tags = Vorschläge für KI, keine Verbote.
Neue pending-Ordner (⚠ PENDING) nach Korrespondenten-Freigabe automatisch angelegt.

**Neuer Ordner:** Format `Hauptordner/Unterordner` → Storage Path in Paperless wird automatisch erstellt.

Suchfeld filtert nach Pfad, Beschreibung, erlaubten Tags, erlaubten Dokumenttypen.

---

## 10. Familie

Zentrale Haushaltskonfiguration — wird von der Pipeline dynamisch geladen.
**Kein Hardcoding** im Code — alles über diesen Tab pflegbar.

### Bereich 1: Haushalt & Personen

**Haushaltsname** erscheint im LLM-Prompt (z.B. «Klassifiziere dieses Dokument für Haushalt Muster, Schweiz»).

**Personen** definieren den Ordner-Namensraum:
- **ID** — interner Schlüssel (Kleinbuchstaben, keine Leerzeichen, eindeutig)
- **Anzeigename** — für Logs und UI
- **Ordner-Prefix** — erster Teil aller Ablage-Pfade (z.B. «Thomas» → `Thomas/Auto`, `Thomas/Steuern`)

> Zuerst Personen speichern — dann können Referenzen erfasst werden.
> Person kann nicht gelöscht werden solange Referenzen damit verknüpft sind.

### Bereich 2: Referenzen (Kennzeichen)

Jede Referenz in `family.json` (`fahrzeuge[]`) steuert **immer** das Custom Field «Auto-Kennzeichen» und die **Person** (Vision oder OCR).

**Ordner-Routing ist optional** («Ordner autom.» / `routing_ordner`):

| Einstellung | Verhalten |
|---|---|
| Ordner autom. **aus** | CF + Person gesetzt; Ordner/Dokumenttyp über Korrespondent, Beziehungen oder LLM (z. B. Versicherungspolice) |
| Ordner autom. **an** | Zusätzlich deterministisches Pre-Routing in den Ziel-Ordner (Garage, MFK, Werkstatt) — kein LLM |

- **Referenz** — Kennzeichen/ID, Pflicht, eindeutig; muss als Option im Paperless-Select «Auto-Kennzeichen» existieren
- **Kategorie** — frei pflegbar in `fahrzeug_kategorien` (nur Anzeige/Hilfe in der UI, kein Pipeline-Routing)
- **Person** — Pflicht, aus gespeicherten Personen
- **Ziel-Ordner** — nur bei aktivem «Ordner autom.», Format `Person/Kategorie` (z. B. `Monika/Auto`)

> Mofas mit gemeinsamem Schild: ein Eintrag, Person setzen, **Ordner autom. aus** — Versicherungsdokumente sollen nicht in `Person/Auto` landen.
> Policen- und Vertragsnummern gehören in **Beziehungen**, nicht in Referenzen.

### Bereich 3: Beziehungen (Stufe 1)

Pro Korrespondent in **Familie → Beziehungen** (gespeichert in `correspondents.json`):

| Feld | Bedeutung |
|---|---|
| **Ref-Nr** | Kunden-/Police-/Vertragsnummer — **muss im Dokument vorkommen** (OCR, Regex-Extraktion oder Vision-Feld Police/Kunde/Rechnung) |
| **Person** | Ordner-Namensraum + CF «Person» bei Match |
| **Dokumenttypen** | Bei genau einem Typ → deterministisch; sonst LLM wählt aus der Liste |
| **Ordner** | Ziel-Speicherpfad bei Ref-Match |
| **Stichworte** | Optional — Tiebreaker wenn **mehrere** Beziehungen dieselbe Ref-Nr. haben (Substring in OCR/Vision, z. B. `prämienrechnung` vs. `versicherungsschein`) |

**Tiebreaker-Reihenfolge** bei gleicher Ref-Nr.: Stichworte → `dokumenttyp_visuell`/Synonyme → LLM.

- Hat eine Beziehung eine **Ref-Nr**, matcht Stufe 1 **nur**, wenn diese Nummer im Dokument steht — **nicht** allein weil es die einzige Beziehung ist oder der Empfänger passt.
- Mehrere Beziehungen pro Korrespondent sind normal (z. B. Thomas mit Kunden-Nr., Monika mit Police-Nr. bei derselben Versicherung).

**Person-CF — Priorität (ab pipe 12.22):**

1. **Kennzeichen** aus `family.json` (Fahrzeugbezug schlägt Empfänger auf der Police)
2. **Beziehung** per Ref-Match
3. Korrespondent / LLM

`Standard-Dokumenttyp` und `Typischer Ordner` am Korrespondenten sind **Fallbacks** für LLM — sie überschreiben keine Beziehung und kein Kennzeichen.

---

## 11. Scan-Workflow

1. Dokumente mit Trennseiten auf Scanner legen
2. Scan starten (Profil «paperless»)
3. ~1–2 Minuten pro Dokument warten
4. paper.manager öffnen — rote Badges zeigen offene Einträge
5. **Korrespondenten Review** → freigeben oder ablehnen
6. **Dokument-Review** → bestätigen oder korrigieren
7. **Manifest** → neue pending-Ordner ergänzen

7. **Manifest** → neue pending-Ordner ergänzen

---

## 13. Brillenpass

Optiker-Rechnungen, Quittungen, Brillenpass-Karten und Augenarzt-Verordnungen werden in **Brillenwerte** (Fern/Nähe, Glas) übersetzt und pro Person versioniert in `brillenpaesse.json` gespeichert — **immer nach Review**.

### Automatischer Workflow (neue Scans)

Voraussetzungen:

1. Korrespondent mit `brillenpass.aktiv` + **Vendor** (siehe Abschnitt 5)
2. Person eindeutig (`family.json` / OCR / Vision)
3. Dokument enthält erkennbare Glaswerte

Ablauf:

```
Scan → post_consume erkennt Optiker-Dokument
  → Auto-Parser wählt Format (Rechnung A4 vs. Karte vs. Verordnung)
  → Vision ergänzt Lücken
  → pending_brillenpass.jsonl + Tag pending_brillenpass
  → Tab Brillenpass → Review → Freigabe → brillenpaesse.json
```

### Format-Erkennung (Auto-Parser)

Pro Vendor werden **Kandidaten-Parser** geladen; das System wählt **einen** passenden Parser:

| Dokument | Parser-Beispiel |
|---|---|
| Fielmann A4-Rechnung | `fielmann_rechnung` |
| Fielmann Brillenpass-Karte | `fielmann_brillenpass` |
| McOptic Quittung/Krankenkassenexemplar | `mcoptic_rechnung` |
| McOptic Karte (SPH/ZYL/ACHSE) | `mcoptic_brillenpass` |
| Augenarzt-Verordnung | `augenarzt_verordnung` |
| Optik Meyer Rechnung/Verordnung | `optik_meyer_rechnung` |

Erkennung über OCR-Heuristik + Vision (`dokumenttyp_visuell`, Layout). Du musst **nicht** manuell zwischen Rechnung und Pass wählen.

### Dedup (gleiche Periode)

Wenn innerhalb von **21 Tagen** (`BRILLENPASS_DEDUP_DAYS`) ein zweites Dokument derselben Person vom gleichen Optiker freigegeben wird (z. B. Rechnung + Pass wenige Tage auseinander), wird die **bestehende Version angereichert** statt ein Duplikat angelegt.

Neue Brille ~12 Monate später → neuer Eintrag mit Diff zur Vorversion.

### Tab Brillenpass — Bereiche

**Übersicht** — alle Personen mit gespeicherten Versionen und offenen Reviews (Badge in Sidebar).

**Manuelle Erfassung** — Werte ohne Scan eintragen (Person, Korrespondent, Datum, Parser optional).

**Aus Dokument parsen** — Paperless-Dok-ID + optional Parser → Felder vorfüllen (ohne Review-Queue).

**Nachträglich verarbeiten** — bestehendes Paperless-Dokument durch Pipeline (Dok-ID, optional Parser-Override).

- Läuft **im Hintergrund** (~1–2 Min Vision); Statuszeile aktualisiert sich per Polling.
- **«Erneut»** ankreuzen, wenn dasselbe Dok schon in der Review-Liste steht (ersetzt offenen Eintrag).
- Bei Fehler: rote Meldung in der UI; Details in `journalctl -u correspondent-manager` und `audit_log.jsonl`.

**Review-Panel** — Vorschlag prüfen, Diff zur letzten Version, Freigeben oder Ablehnen.

### Unterstützte Optiker (Stand pipe 12.44)

| Vendor | Formate |
|---|---|
| `fielmann` | Rechnung + Brillenpass-Karte |
| `mcoptic` | Rechnung/Quittung + Brillenpass-Karte |
| `optik_meyer` | Rechnung/Verordnung |
| `augenarzt` | Verordnung |

---

## 14. Legacy QR-Split

Für **alte NAS-Mehrseiten-Scans** mit QR-Codes auf Trennseiten (nicht Swiss QR-Bill, nicht Paperless-PATCHT).

Typischer QR-Inhalt: `060102_Gesundheit_Monika` (Regex: `^[0-9]{6}_[^\s]+$`).

### Wann nutzen?

- Ein Paperless-Dokument enthält **viele Einzeldokumente** in einem PDF
- Jede Trennseite hat einen **Metadaten-QR** aus der alten Scan-Pipeline
- Dokument ist bereits in Paperless (Legacy-Import oder falsch zusammengeführt)

### Ablauf in paper.manager

Menü **✂ Legacy QR-Split**:

1. **Paperless Dok-ID** eingeben (z. B. `651`)
2. **Vorschau** — zeigt Seitenbereiche und erkannte Barcodes (dry-run, schreibt nichts)
3. **Splitten → consume** — PDF wird von Paperless geladen, gesplittet, Teile nach `PAPERLESS_CONSUME_DIR` gelegt
4. Paperless Consumer startet die **normale Pipeline** pro Teil (OCR, Vision, Klassifizierung)

> Das **Original-Dokument** in Paperless bleibt unverändert. Nach erfolgreichem Split ggf. manuell archivieren oder taggen.

### `.env` auf CT 121

```bash
PAPERLESS_CONSUME_DIR=/mnt/paperless-data/consume
LEGACY_SPLIT_QR_REGEX=^[0-9]{6}_[^\s]+$
```

Optional: `BRILLENPASS_DEDUP_DAYS=21` (Achtung Schreibweise — nicht `RILLENPASS_…`).

### Abgrenzung

| Mechanismus | Zweck |
|---|---|
| `pre_consume_qr.py` | Swiss **QR-Rechnung** (SPC) beim **neuen** Scan |
| `legacy_split_by_qr.py` | **Metadaten-QR** auf Trennseiten — nachträglich per Dok-ID |
| `legacy-import-batch.sh` | NAS-Bulk ohne OCR/Pipeline (nur Index) |

Details Bulk-Import: [`LEGACY_IMPORT.md`](LEGACY_IMPORT.md)

---

## 12. Dokumente in Paperless finden

| Suchanfrage | Filter |
|---|---|
| Offene Rechnungen | Custom Field `Status` = `Offen` |
| Bezahlte Rechnungen | Custom Field `Status` = `Bezahlt` |
| Zahllauf vom 06.02.2026 | Custom Field `Bezahlt am` = `2026-02-06` |
| Heute gescannt | Custom Field `Gescannt am` = heute |
| Vollautomatisch verarbeitet | Custom Field `Verarbeitung` = `auto STP` |
| Dokumente für Monika | Custom Field `Person` = `Monika` |
| Steuerbelege 2025 | Tag = `Steuerrelevant` + Datum 2025 |
| Absender X | Korrespondent = «X» |

---

## Schnellreferenz

### Tastaturkürzel

| Taste | Funktion |
|---|---|
| Enter | Tag/Synonym/Keyword hinzufügen |
| Backspace | Letzten Tag löschen (leeres Feld) |
| × | Tag entfernen |
| Klick auf Toast | Meldung schliessen |
| Ctrl+Shift+R | Browser Hard-Refresh |

### Bezahlt-Vermerke

```
bez. 6.2.26     → 06.02.2026  ✓
bez 26.3.26     → 26.03.2026  ✓
BEZ 6.2.26      → 06.02.2026  ✓
bezahlt 6.2.26  → 06.02.2026  ✓
bz. 6.2.26      → 06.02.2026  ✓
EZ 26.3.26      → nicht erkannt (Einzahlung)  ✗
```

### Custom Fields

| ID | Feld | Typ | Quelle |
|---|---|---|---|
| 1 | CHF | Monetär | QR-Bill |
| 5 | Rechnungsnummer | Text | QR-Bill/Vision |
| 6 | Kundennummer | Text | Vision |
| 7 | QR-Referenz | Text | QR-Bill |
| 8 | Fällig am | Datum | QR-Bill |
| 9 | Status | Auswahl | Automatisch |
| 10 | Policennummer | Text | Vision |
| 11 | Auto-Kennzeichen | Auswahl | Vision/OCR + family.json |
| 15 | Person | Auswahl | family.json bei Kennzeichen-Match oder Beziehung |
| 12 | Bezahlt am | Datum | Handschrift bez. |
| 13 | Gescannt am | Datum | Immer = heute |
| 14 | Verarbeitung | Auswahl | `auto STP` wenn ohne Review fertig |
