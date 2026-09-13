# paper.manager — Benutzerhandbuch

**Version 3.22 | September 2026** (Pipeline `12.81`, Backend `2.69`, UI `3.22`)

> Entwickler-Details: [`DEVELOPER.md`](DEVELOPER.md) · Legacy-Import: [`LEGACY_IMPORT.md`](LEGACY_IMPORT.md)

---

## Inhaltsverzeichnis

1. [Auf einen Blick](#1-auf-einen-blick)
2. [Täglicher Workflow](#2-taeglicher-workflow)
3. [Korrespondenten Review](#3-korrespondenten-review)
4. [Korrespondenten verwalten](#4-korrespondenten-verwalten)
5. [Dokument-Review](#5-dokument-review)
6. [Dokumenttypen](#6-dokumenttypen)
7. [Tags](#7-tags)
8. [Manifest (Ordner)](#8-manifest-ordner)
9. [Familie (Haushalt und Zuordnung)](#9-familie-haushalt-und-zuordnung)
10. [Handschrift und HTR](#10-handschrift-und-htr)
11. [Brillenpass](#11-brillenpass)
12. [Legacy QR-Split](#12-legacy-qr-split)
13. [Pipeline nachholen](#13-pipeline-nachholen)
14. [Dokumente in Paperless finden](#14-dokumente-in-paperless-finden)
15. [Fehlerbehebung](#15-fehlerbehebung)
16. [Schnellreferenz](#16-schnellreferenz)

---

## 1. Auf einen Blick

paper.manager ist die Review- und Verwaltungsoberfläche für die automatische Dokumentklassifizierung mit Paperless-NGX. Nach jedem Scan analysiert ein lokales KI-System (Vision + LLM) das Dokument vollständig als Bild — erkennt Absender, Datum, Betrag, Kennzeichen und handschriftliche Notizen.

> 💡 **Tipp:** Bezahlte Rechnungen mit `bez. 6.2.26` oben rechts markieren — das System setzt automatisch `Status=Bezahlt` und `Bezahlt am=06.02.2026`. Details: [Abschnitt 10](#10-handschrift-und-htr).

### Zugang

| Zugang | URL | Auth |
|---|---|---|
| Via Domain (empfohlen) | `https://paperless.example.app/corr-manager/` | Authentik + Paperless-Session (gleiche Domain) |
| Paperless-Dashboard | Kein eingebauter Link — **neuer Tab:** `/corr-manager/` | Wie Domain-Zeile |
| Via interne IP | `http://192.168.131.31:8100` | Paperless-Login auf **derselben IP** (`:8000`) |

> **Pfad:** `/corr-manager/` mit **Bindestrich** — nicht `corr.manager` (Punkt).

Direkt nach dem Öffnen (ohne Hash in der URL) landest du in **Korrespondenten Review** (`#pending`) — dort warten in der Regel die dringendsten Einträge.

### Start-Tab (Home)

Menü **⌂ Start** oder Klick auf **«paper.manager»** (Logo):

- **Aktive Versionen** (UI, Backend, Pipeline, pre OCR, pre QR, Ollama-Modelle)
- **Dieses Benutzerhandbuch**, direkt in der UI lesbar
- Download **Bedienungsanleitung.docx** (Word, zum Ausdrucken)
- Kurzüberblick Pipeline, Custom Fields, Vorher/Nachher

### Navigation

Jeder Menüpunkt hat einen eigenen URL-Hash — Lesezeichen und Browser-Zurück funktionieren.

| Menüpunkt | Hash | Funktion | Handbuch |
|---|---|---|---|
| ⌂ Start | `#home` | Landing Page — Versionen, Handbuch, Systemübersicht | — |
| ! Korrespondenten Review | `#pending` | Neue Absender freigeben | [§3](#3-korrespondenten-review) |
| # Korrespondenten | `#correspondents` | Absender verwalten | [§4](#4-korrespondenten-verwalten) |
| D Dokument-Review | `#docreview` | Unsichere Dokumente prüfen, Vorschau + LLM-Begründung | [§5](#5-dokument-review) |
| T Dokumenttypen | `#doctypes` | Synonyme + Ausschluss-Keywords | [§6](#6-dokumenttypen) |
| ~ Tags | `#tags` | Tags + Ausschluss-Keywords | [§7](#7-tags) |
| M Manifest | `#manifest` | Ordner konfigurieren | [§8](#8-manifest-ordner) |
| 👪 Familie | `#family` | Haushalt, Personen, Referenzen (Kennzeichen), Beziehungen | [§9](#9-familie-haushalt-und-zuordnung) |
| ✍ Handschrift | `#handschrift` | HTR nachträglich starten, Profil wählen, Pipeline-Erklärung | [§10](#10-handschrift-und-htr) |
| 👓 Brillenpass | `#brillenpass` | Optiker-Dokumente parsen, Review, versionierter Pass pro Person | [§11](#11-brillenpass) |
| ✂ Legacy QR-Split | `#legacy-split` | Mehrseiten-Scans nachträglich an QR splitten → `consume/` | [§12](#12-legacy-qr-split) |
| ↻ Pipeline | `#pipeline` | Volle `post_consume`-Klassifizierung für bestehende Dokumente nachholen | [§13](#13-pipeline-nachholen) |

Alle Tabs mit Listen haben ein **Suchfeld** für live Filterung.

> ℹ️ Versionsnummern (Sidebar kompakt, Start-Tab vollständig), was tun bei falscher Anzeige, Logs & Neustart → [Abschnitt 15](#15-fehlerbehebung).

---

## 2. Täglicher Workflow

So läuft der Alltag mit paper.manager ab — vom Scanner bis zum abgelegten Dokument:

1. Dokumente mit Trennseiten auf den Scanner legen
2. Scan starten (Profil «paperless»)
3. ~1–2 Minuten pro Dokument warten (OCR + Pipeline laufen automatisch)
4. paper.manager öffnen — rote Badges in der Sidebar zeigen offene Einträge
5. **Korrespondenten Review** ([§3](#3-korrespondenten-review)) → neue Absender freigeben oder ablehnen
6. **Dokument-Review** ([§5](#5-dokument-review)) → Ergebnis bestätigen oder korrigieren
7. **Manifest** ([§8](#8-manifest-ordner)) → neue pending-Ordner ergänzen, falls welche entstanden sind
8. **Brillenpass / Handschrift** ([§11](#11-brillenpass), [§10](#10-handschrift-und-htr)) → nur falls dort Badges offen sind

Für die meisten Scans reicht Schritt 1–6 — Schritt 7 und 8 kommen nur bei neuen Ordnern bzw. Spezialdokumenten vor.

### QS-Modus (Qualitätssicherung)

Ein Schalter oben in der Sidebar bestimmt, wie streng geprüft wird:

| Status | Bedeutung |
|---|---|
| AUS (grau) — Nur unsichere | Nur Dokumente mit niedriger KI-Konfidenz landen in der Queue |
| EIN (grün) — QS-Modus aktiv | ALLE gescannten Dokumente landen zur Prüfung in der Queue |

Der Status wird serverseitig gespeichert und bleibt nach einem Refresh erhalten. Sinnvoll z. B. direkt nach einem grossen Scan-Batch, um jedes Dokument einmal zu sehen — danach wieder ausschalten.

---

## 3. Korrespondenten Review

Trifft die Pipeline auf einen unbekannten Absender, landet er hier in der Warteschlange (roter Badge) — bevor irgendein Dokument diesem Absender zugeordnet wird.

### Felder beim Freigeben

| Feld | Bedeutung |
|---|---|
| Kanonischer Name | Offizieller Absender-Name |
| **Kürzel** | 2–6 Zeichen, eindeutig (z. B. `UBS`) — erscheint im Titel-Suffix und als Badge |
| Standard-Dokumenttyp | Typischer Dokumenttyp dieses Absenders |
| Varianten | Alternative Schreibweisen (Enter / ×) |
| Match-Strings | Suchbegriffe für Paperless-Matching |
| Typische Ordner | Format: Hauptordner/Unterordner |
| Notiz | Interne Bemerkung |

### Aktionen

| Aktion | Wirkung |
|---|---|
| **Freigeben** | Korrespondent wird in Paperless + `correspondents.json` angelegt; neue Ordner erscheinen als PENDING im Manifest |
| **Ablehnen** | Eintrag wird verworfen; betroffene Dokumente erhalten `pending_review` und erscheinen im **Dokument-Review** |
| **⇔ Merge** | Nur bei Fuzzy-Match (ähnlicher Name bereits in der Map); führt Duplikate vor der Freigabe zusammen |
| **Als NEU anlegen statt Merge** | Wechselt zu einem neuen Korrespondenten mit vollem Formular (UID, IBAN, SWIFT, E-Mail, Telefon) — sinnvoll, wenn der Fuzzy-Vorschlag falsch ist (z. B. ähnliche Banknamen) |

> ⚠️ Tags werden **nicht** auf Korrespondenten-Ebene gepflegt — dafür ist Abschnitt [§7 Tags](#7-tags) zuständig.

---

## 4. Korrespondenten verwalten

Laufende Pflege bestehender Absender: Edit-Button beim jeweiligen Eintrag öffnet Standard-Dokumenttyp, Varianten, Match-Strings, Typische Ordner, Notiz, **Identifikatoren** (UID, IBAN, SWIFT/BIC, E-Mail, Telefon), **Kürzel** und **Platzhalter**.

Das Suchfeld filtert nach Name, Varianten, Match-Strings, Ordnern und Notiz.

### Platzhalter-Korrespondenten

Für Dokumente **ohne echten Absender** (Impfpass, Privatnotizen, anonyme Belege) legst du selbst generische Korrespondenten an und markierst sie als Platzhalter:

| Beispiel | Zweck |
|---|---|
| `Gesundheit` | Impfpass, Arztbriefe ohne klaren Absender |
| `Medien` | Zeitungsausschnitte, Broschüren |
| `Privat` | Handschrift, Notizen ohne Absender |

| UI-Element | Verhalten |
|---|---|
| Checkbox **Platzhalter** (Edit) | Kennzeichnet den Eintrag; Badge **Platzhalter** in Liste und Picker |
| Filter oben | **Alle** / **Nur Platzhalter** / **Ohne Platzhalter** |
| Batch-Modus | Mehrere Einträge wählen → **Als Platzhalter markieren** / **Markierung entfernen** |

**Pipeline-Verhalten:** Platzhalter werden **nicht** automatisch per OCR, Fuzzy-Match oder Identifikator zugeordnet. Du wählst sie manuell im Dokument-Review (z. B. Impfpass → `Gesundheit` statt fälschlich erkanntem Kantonsnamen).

> Platzhalter brauchen keinen Match-String — optional nur Kürzel und Standard-Dokumenttyp.

### Identifikatoren (UID, IBAN, …)

Unter **Erweitert** pro Korrespondent: UID, IBAN, SWIFT/BIC, E-Mail, Telefon. Die Pipeline nutzt sie für deterministische Zuordnung (Priorität: UID → IBAN → SWIFT → E-Mail → Telefon).

| Feld | Details |
|---|---|
| **IBAN** | Beim Speichern prüft das Backend Modulo-97 und Länderlänge; ungültige IBANs werden abgelehnt. Die Pipeline extrahiert nur echte IBANs (keine OCR-Falschtreffer wie «CHRISTO…» oder «CHE…» ohne Prüfziffer). |
| **Telefon** | Extraktion über `phonenumbers` (CH/DE/AT/…), inkl. Formate wie `+41 (0) 61 …` und nationale `061 …`. UID-Ziffernfolgen werden nicht als Telefon vorgeschlagen. |
| **UID / MWST** | In der Schweiz ist die UID-Nummer identisch mit der MWST-Nummer (`CHE-xxx.xxx.xxx`) — «MWST» ist nur ein Zusatzlabel, im Review erscheint dieselbe Nummer höchstens einmal. |
| **SWIFT/BIC** | 8 oder 11 Zeichen, z. B. aus QR-Rechnung oder explizit gelabelter Zeile (`SWIFT:` / `BIC:`). Fliesstext-Wörter wie «RECHNUNG» oder «MARKETPLACE» werden nicht mehr als SWIFT vorgeschlagen. |
| Deutsche Steuernummer | Bewusst **nicht** im Scope (z. B. `110/106/07177`) — Fokus liegt auf CH-Identifikatoren. |

### Brillenpass am Korrespondenten (Optiker)

Für Optiker, Augenärzte und ähnliche Absender, deren Dokumente Brillenwerte enthalten:

| Feld | Bedeutung |
|---|---|
| **Brillenpass aktiv** | Pipeline versucht automatisch, Glaswerte zu extrahieren |
| **Optiker (Vendor)** | z. B. `fielmann`, `mcoptic`, `optik_meyer`, `augenarzt` — das System erkennt automatisch, ob Rechnung (A4) oder Brillenpass-Karte |
| **Typische Begriffe** | OCR-Hilfen für die Erkennung (optional) |
| **Erweitert: Parser** | Nur bei Bedarf manuell einschränken (sonst leer lassen) |

Beispiel McOptic in `correspondents.json`:

```json
"brillenpass": {
  "aktiv": true,
  "vendor": "mcoptic",
  "typische_begriffe": ["McOptic", "Quittung", "SPH ZYL"]
}
```

Details zur Brillenpass-Pipeline: [Abschnitt 11](#11-brillenpass).

### Korrespondenten zusammenführen

**⇔ Merge** führt zwei Einträge zusammen — alle Dokumente werden umgeschrieben, das Duplikat wird gelöscht.

---

## 5. Dokument-Review

Dokumente mit einem der pending-Tags landen automatisch in der Review-Warteschlange:

| Tag | Farbe | Bedeutung |
|---|---|---|
| `pending_review` | gelb | KI unsicher, Datum verdächtig, Fallback-Ordner |
| `pending_qs` | grün | QS-Modus aktiv — alle Dokumente werden geprüft |
| `pending_new_correspondent` | rot | Unbekannter Absender — erscheint auch hier (Grund: «Korrespondent offen») |

### Panel-Aufbau (30/70)

**Links (ca. 30 %) — Vorschau**
- Grosses Thumbnail der ersten Seite (Proxy → Paperless-API)
- Klick öffnet das PDF in neuem Tab (Proxy → `/api/proxy/document/{id}/preview/`)
- Funktioniert auch bei Zugriff über interne IP ohne Authentik

**Rechts (ca. 70 %) — KI-Erkennung + Korrektur**

| Feld | Beschreibung |
|---|---|
| Titel | Vom LLM vorgeschlagen — bei Freigeben editierbar und speicherbar |
| Korrespondent | Erkannter Absender — Picker (Suchfeld), zeigt nur **freigegebene** Korrespondenten; Kürzel- und Platzhalter-Badges sichtbar |
| Ordner | Zugewiesener Speicherpfad |
| Dokumenttyp | Erkannter Dokumenttyp |
| Belegdatum | Ausstellungsdatum (`tt.mm.jjjj`), gespeichert als Paperless-Feld `created` |
| Confidence | Farbig: grün ≥90 %, gelb 70–89 %, rot <70 % |
| Review-Grund | Warum das Dokument in die Queue kam |
| LLM-Begründung | Erklärung des LLM zur Einschätzung |

> ℹ️ **Belegdatum-Logik (ab Pipe 12.81):** Vision und LLM haben Vorrang, wenn sie übereinstimmen. Verdächtig alte OCR-Treffer (>2 Jahre, oft Rauschen aus UID/Telefon) werden verworfen, nicht mehr blind übernommen.

Unter den KI-Feldern:
- **Tags als Chips** — alle gesetzten Tags
- **Custom Fields** — nur gefüllte Felder; Status-ID wird lesbar als «Bezahlt»/«Offen» dargestellt

### Korrekturen (2×2-Grid)

| Feld | Funktion |
|---|---|
| Ordner | Anderen Speicherpfad wählen |
| Korrespondent | Anderen Absender wählen (Picker mit Badges; Platzhalter z. B. für Impfpass ohne Absender) |
| Dokumenttyp | Anderen Typ wählen |
| Tags | Tags wählen — bei **Neu klassifizieren** werden bestehende Tags **ersetzt** (nicht angehängt) |

### Aktionen

| Aktion | Wirkung |
|---|---|
| **✓ Freigeben** | Entfernt pending-Tags; Titel, Belegdatum, Tags und Custom Fields werden gespeichert |
| **✎ Neu klassifizieren** | Wendet Korrespondent, Ordner, Dokumenttyp und Tags an; trainiert Manifest + Korrespondenten-Modell |
| **✗ Ignorieren** | Entfernt das Dokument aus der Queue, ohne etwas zu ändern |

> Datumsformat in der UI: **Schweizer Schreibweise** `tt.mm.jjjj` (nicht US-Format).

---

## 6. Dokumenttypen

### Feldprofil (Custom Fields pro Typ)

Im Edit-Dialog jedes Dokumenttyps: Tabelle **Extrahieren / Im Review / Pflicht** pro Custom Field.

| Spalte | Bedeutung |
|---|---|
| **Extrahieren** | Pipeline/OCR befüllt dieses Feld nur, wenn angehakt |
| **Im Review** | Feld ist im Dokument-Review-Formular sichtbar |
| **Pflicht** | Muss vor Freigabe gesetzt sein (Review-Hinweis) |

Die Pipeline-Felder **Verarbeitung** und **Person** werden unabhängig vom Feldprofil gesetzt (siehe [§9](#9-familie-haushalt-und-zuordnung) und die Custom-Field-Tabelle in [§16](#16-schnellreferenz)).

### Handschrift (HTR)

Dropdown **Handschrift (HTR):** `auto` | `default` | `schulbericht` | `off` — steuert die mehrstufige Transkriptions-Pipeline ([§10](#10-handschrift-und-htr)). In der Typenliste erscheint ein grünes **HTR**-Badge, wenn nicht `auto`.

### Synonyme

Global einmalig (Unique-Constraint). Enter zum Hinzufügen, × oder Backspace zum Entfernen. Direkt auf «Speichern» klicken ohne Enter übernimmt den getippten Wert automatisch.

### Ausschluss-Keywords

Pro Typ definierbar — kommt das Keyword im Dokument vor, wird dieser Typ **nicht** zugewiesen. Nicht Unique-pflichtig (gleiches Keyword bei mehreren Typen erlaubt).

> Beispiel: Dokumenttyp `Servicerechnung` + Ausschluss `Strassenverkehrsamt` → Verkehrssteuern werden nie als Servicerechnung erkannt.

Das Suchfeld filtert nach Name, Beschreibung, Synonymen und Ausschluss-Keywords.

---

## 7. Tags

Cards mit Dokumentanzahl und Ausschluss-Keywords. Edit öffnet den Bearbeitungsdialog.

| Aktion | Verhalten |
|---|---|
| **Umbenennen** | Neuen Namen eingeben → Speichern → wirkt direkt in Paperless |
| **Ausschluss-Keywords** | Kommt das Keyword im Dokument vor, wird der Tag nicht gesetzt |

> Beispiel: Tag `Service` + Ausschluss `Strassenverkehrsamt` → kein «Service»-Tag bei Verkehrssteuern.

---

## 8. Manifest (Ordner)

Das Manifest verwaltet die Ordnerstruktur (Storage Paths), in die Dokumente abgelegt werden.

- **Erlaubte Tags** = Vorschläge für die KI, keine Verbote.
- **Neue pending-Ordner** (⚠ PENDING) werden nach einer Korrespondenten-Freigabe automatisch angelegt — hier bestätigen oder anpassen.
- **Neuer Ordner:** Format `Hauptordner/Unterordner` → der Storage Path wird in Paperless automatisch erstellt.

Das Suchfeld filtert nach Pfad, Beschreibung, erlaubten Tags und erlaubten Dokumenttypen.

---

## 9. Familie (Haushalt und Zuordnung)

Zentrale Haushaltskonfiguration — wird von der Pipeline dynamisch geladen. **Kein Hardcoding** im Code: alles ist über diesen Tab pflegbar. Drei Bereiche bauen aufeinander auf.

### 9.1 Haushalt und Personen

Der **Haushaltsname** erscheint im LLM-Prompt (z. B. «Klassifiziere dieses Dokument für Haushalt Muster, Schweiz»).

**Personen** definieren den Ordner-Namensraum:

| Feld | Bedeutung |
|---|---|
| **ID** | Interner Schlüssel (Kleinbuchstaben, keine Leerzeichen, eindeutig) |
| **Anzeigename** | Für Logs und UI |
| **Ordner-Prefix** | Erster Teil aller Ablage-Pfade (z. B. «PersonA» → `PersonA/Auto`, `PersonA/Steuern`) |

> Zuerst Personen speichern — erst danach können Referenzen erfasst werden. Eine Person kann nicht gelöscht werden, solange Referenzen mit ihr verknüpft sind.

### 9.2 Referenzen (Kennzeichen)

Jede Referenz in `family.json` (`fahrzeuge[]`) steuert **immer** das Custom Field «Auto-Kennzeichen» und die **Person** (via Vision oder OCR).

**Ordner-Routing ist optional** («Ordner autom.» / `routing_ordner`):

| Einstellung | Verhalten |
|---|---|
| Ordner autom. **aus** | CF + Person werden gesetzt; Ordner/Dokumenttyp laufen über Korrespondent, Beziehungen oder LLM (z. B. Versicherungspolice) |
| Ordner autom. **an** | Zusätzlich deterministisches Pre-Routing in den Ziel-Ordner (Garage, MFK, Werkstatt) — kein LLM |

| Feld | Bedeutung |
|---|---|
| **Referenz** | Kennzeichen/ID, Pflicht, eindeutig; muss als Option im Paperless-Select «Auto-Kennzeichen» existieren |
| **Kategorie** | Frei pflegbar in `fahrzeug_kategorien` (nur Anzeige/Hilfe in der UI, kein Pipeline-Routing) |
| **Person** | Pflicht, aus gespeicherten Personen |
| **Ziel-Ordner** | Nur bei aktivem «Ordner autom.», Format `Person/Kategorie` (z. B. `PersonB/Auto`) |

> Mofas mit gemeinsamem Schild: ein Eintrag, Person setzen, **Ordner autom. aus** — Versicherungsdokumente sollen nicht in `Person/Auto` landen.
> Policen- und Vertragsnummern gehören in **Beziehungen** (9.3), nicht in Referenzen.

### 9.3 Beziehungen

Pro Korrespondent unter **Familie → Beziehungen** gepflegt (gespeichert in `correspondents.json`):

| Feld | Bedeutung |
|---|---|
| **Ref-Nr** | Kunden-/Police-/Vertragsnummer — **muss im Dokument vorkommen** (OCR, Regex-Extraktion oder Vision-Feld Police/Kunde/Rechnung) |
| **Person** | Ordner-Namensraum + CF «Person» bei Match |
| **Dokumenttypen** | Bei genau einem Typ → deterministisch; sonst wählt das LLM aus der Liste |
| **Ordner** | Genau **ein** Ziel-Speicherpfad bei Ref-Match (die UI erlaubt nur einen Ordner) |
| **Stichworte** | Tiebreaker, wenn mehrere Beziehungen dieselbe Ref-Nr. haben (Substring in OCR/Vision) |

**Tiebreaker-Reihenfolge** bei gleicher Ref-Nr.: Stichworte → `dokumenttyp_visuell`/Synonyme → LLM.

- Hat eine Beziehung eine **Ref-Nr**, matcht sie **nur**, wenn diese Nummer im Dokument steht — nicht allein, weil es die einzige Beziehung ist oder der Empfänger passt.
- Mehrere Beziehungen pro Korrespondent sind normal (z. B. verschiedene Personen mit je eigener Kunden-Nr. beim selben Versicherer).
- **Wichtig:** Mehrere erlaubte Dokumenttypen auf **einer** Beziehung ändern nur den Typ (LLM wählt) — der Ordner bleibt derselbe. Policen und Rechnungen mit derselben Police-Nr. brauchen deshalb **zwei Beziehungen**.

#### Beispiel: Police und Rechnung mit gleicher Ref-Nr.

Typischer Fall: Dieselbe Vertragsnummer steht auf dem Versicherungsschein **und** auf der Prämienrechnung — ein Ordner pro Beziehung, also zwei Zeilen pflegen.

Korrespondent «Muster Leben AG», Ref-Nr. `8.123.456`, Person «Alex»:

| | Beziehung A — Police | Beziehung B — Rechnung |
|---|---|---|
| Bezeichnung | z. B. Leben #8.123.456 Police | z. B. Leben #8.123.456 Rechnung |
| Ref-Nr | `8.123.456` | `8.123.456` (identisch) |
| Person | Alex | Alex (oder Zahler/Empfänger, falls anders) |
| Erlaubte Doktypen | Steuerwertbescheinigung, Versicherungsabrechnung (ohne Rechnung) | Rechnung, Versicherungsabrechnung |
| Ordner | `Familie/Versicherung/Policen` | `Familie/Versicherung/Rechnungen` |
| Stichworte | `police`, `steuerwert`, `bescheinigung`, `überschuss` | `rechnung`, `prämie`, `zahlteil`, `einzahlung` |

Beim Scan einer Prämienrechnung matchen beide Zeilen über die Ref-Nr. → Stichworte/Vision (`dokumenttyp_visuell=Rechnung`) wählen Beziehung B → Ordner **Rechnungen**.

> **Falsch:** Nur eine Beziehung mit Ordner `…/Policen` und Doktypen inkl. «Rechnung» — die Datei landet weiterhin unter Policen, auch wenn der Typ «Rechnung» heisst.
> **Richtig:** Zwei Beziehungen, gleiche Ref-Nr., unterschiedlicher Ordner, Stichworte gesetzt. Ref-Nr. exakt wie im PDF (Bindestriche/Leerzeichen wie im OCR-Text).

**Priorität beim Setzen des CF «Person»** (ab Pipe 12.22):

1. **Kennzeichen** aus `family.json` (Fahrzeugbezug schlägt Empfänger auf der Police)
2. **Beziehung** per Ref-Match
3. Korrespondent / LLM

`Standard-Dokumenttyp` und `Typischer Ordner` am Korrespondenten sind **Fallbacks** für das LLM — sie überschreiben keine Beziehung und kein Kennzeichen.

---

## 10. Handschrift und HTR

### 10.1 Kurznotiz «bezahlt» (Vision Stufe 1)

Jedes Dokument wird zweistufig auf einen Zahlungsvermerk geprüft:

1. **Vision:** `qwen2.5vl:7b` analysiert das Dokument als Bild, sucht Handschrift oben rechts.
2. **Regex:** `parse_handschrift_bezahlt()` extrahiert das Datum daraus.

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

**Gesetzte Custom Fields bei Bezahlt-Vermerk:**

| Feld | Wert |
|---|---|
| Status | `Bezahlt` |
| Bezahlt am | Datum aus Handschrift (für Zahllauf-Abgleich im E-Banking) |
| Gescannt am | immer = heutiges Datum (bei jedem Dokument) |

### 10.2 Mehrstufige HTR (Handschrift-Transkription)

Für längere Handschrift (Schulberichte, Arztberichte) läuft nach der Baseline-Vision optional eine HTR-Pipeline:

```
Vision (Baseline) → Profil wählen → HTR zeilengetreu → (Schulbericht:) Extract → Content + Notiz
```

| Profil (`htr_profiles.json`) | Verhalten |
|---|---|
| `default` | Zeilengetreue Transkription, Trim-Crop |
| `schulbericht` | HTR aller Seiten → strukturierte Felder (Name, Klasse, …) |
| `schulbericht_crop_strong` | Wie Schulbericht, stärkeres horizontales Cropping |

**Konfiguration pro Dokumenttyp** (Tab Dokumenttypen → Edit → Handschrift (HTR)):

| Wert | Bedeutung |
|---|---|
| `auto` | Heuristik (Schulbericht-Erkennung, Handschrift-Signale) |
| `default` | Immer zeilengetreue HTR |
| `schulbericht` | Immer Schulbericht-Pipeline |
| `off` | Kein HTR |

Optional gibt es pro **Korrespondent** einen Override pro Dokumenttyp (`htr_profiles_by_document_type` in `correspondents.json`).

**Content (Strategie D, ab Pipe 12.72):** Bei Schulbericht-HTR ersetzt der Block `--- Handschrift (HTR) ---` den OCR-Text:

1. Metadaten aus **Seite 1** (Schüler, Klasse, Zeitraum, Lehrperson)
2. Darunter Transkript mit `--- Seite N ---` pro PDF-Seite

Die **Notiz am Dokument** ist eine kompakte Zusammenfassung inkl. Arbeitshaltung/Leistungen — kein Volltext-Dump.

**Unsichere Handschrift ohne festes Profil** erhält den Tag `pending_htr_decision` — manuell im Tab **✍ Handschrift** nachverarbeiten.

### 10.3 HTR nachträglich starten

Tab **✍ Handschrift** → Paperless-Dok-ID eingeben → optional Profil wählen → **▶ HTR starten** (1–3 Min/Seite, Status-Polling).

**Wann den Tab öffnen?**
- Dokument wurde vor dem HTR-Deploy gescannt
- Dokument trägt den Tag `pending_htr_decision`
- Ein Schulbericht soll mit anderem Profil getestet werden (`schulbericht` vs. `schulbericht_crop_strong`)

CLI auf CT121: `python3 htr_runner.py <DOK-ID> [--profile schulbericht]`

---

## 11. Brillenpass

Optiker-Rechnungen, Quittungen, Brillenpass-Karten und Augenarzt-Verordnungen werden in **Brillenwerte** (Fern/Nähe, Glas) übersetzt und pro Person versioniert in `brillenpaesse.json` gespeichert — **immer nach Review**.

### 11.1 Automatischer Workflow (neue Scans)

Voraussetzungen:

1. Korrespondent mit `brillenpass.aktiv` + **Vendor** (siehe [§4](#4-korrespondenten-verwalten))
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

### 11.2 Format-Erkennung (Auto-Parser)

Pro Vendor werden Kandidaten-Parser geladen; das System wählt **einen** passenden Parser über OCR-Heuristik + Vision (`dokumenttyp_visuell`, Layout) — du musst nicht manuell zwischen Rechnung und Pass wählen.

| Dokument | Parser-Beispiel |
|---|---|
| Fielmann A4-Rechnung | `fielmann_rechnung` |
| Fielmann Brillenpass-Karte | `fielmann_brillenpass` |
| McOptic Quittung/Krankenkassenexemplar | `mcoptic_rechnung` |
| McOptic Karte (SPH/ZYL/ACHSE) | `mcoptic_brillenpass` |
| Augenarzt-Verordnung | `augenarzt_verordnung` |
| Optik Meyer Rechnung/Verordnung | `optik_meyer_rechnung` |

**Unterstützte Optiker (Stand Pipe 12.60):**

| Vendor | Formate |
|---|---|
| `fielmann` | Rechnung + Brillenpass-Karte |
| `mcoptic` | Rechnung/Quittung + Brillenpass-Karte |
| `optik_meyer` | Rechnung/Verordnung |
| `augenarzt` | Verordnung |

### 11.3 Dedup (gleiche Periode)

Wird innerhalb von **21 Tagen** (`BRILLENPASS_DEDUP_DAYS`) ein zweites Dokument derselben Person vom gleichen Optiker freigegeben (z. B. Rechnung + Pass wenige Tage auseinander), wird die **bestehende Version angereichert** statt ein Duplikat anzulegen.

Eine neue Brille ~12 Monate später erzeugt einen neuen Eintrag mit Diff zur Vorversion.

### 11.4 Tab Brillenpass — Bereiche

| Bereich | Zweck |
|---|---|
| **Übersicht** | Alle Personen mit gespeicherten Versionen und offenen Reviews (Badge in Sidebar). Zeigt Glaswerte aus `messung` oder älterem `fern`-Block. |
| **Manuelle Erfassung** | Werte ohne Scan eintragen (Person, Korrespondent, Datum, Parser optional). |
| **Aus Dokument parsen** | Paperless-Dok-ID + optional Parser → Felder vorfüllen (ohne Review-Queue). |
| **Nachträglich verarbeiten** | Bestehendes Paperless-Dokument durch die Pipeline schicken (Dok-ID, optional Parser-Override). Läuft im Hintergrund (~1–2 Min Vision); Statuszeile aktualisiert per Polling. |
| **Review-Panel** | Vorschlag prüfen, Diff zur letzten Version, Freigeben oder Ablehnen. |

Bei «Nachträglich verarbeiten»: **«Erneut»** ankreuzen, wenn dasselbe Dokument schon in der Review-Liste steht (ersetzt den offenen Eintrag). Bei Fehler erscheint eine rote Meldung in der UI; Details in `journalctl -u correspondent-manager` und `audit_log.jsonl`.

> ℹ️ Zeigt die Übersicht **«Keine Glaswerte»**, obwohl der Review-Vorschlag stimmt (McOptic u. a. speichern in `messung`) → Reparatur siehe [Abschnitt 15](#15-fehlerbehebung).

---

## 12. Legacy QR-Split

Für **alte NAS-Mehrseiten-Scans** mit QR-Codes auf Trennseiten (nicht Swiss QR-Bill, nicht Paperless-PATCHT).

Typischer QR-Inhalt: `060102_Gesundheit_PersonB` (Regex: `^[0-9]{6}_[^\s]+$`).

### Wann nutzen?

- Ein Paperless-Dokument enthält **viele Einzeldokumente** in einem PDF
- Jede Trennseite hat einen **Metadaten-QR** aus der alten Scan-Pipeline
- Das Dokument ist bereits in Paperless (Legacy-Import oder falsch zusammengeführt)

### Ablauf in paper.manager

Menü **✂ Legacy QR-Split**:

1. **Paperless Dok-ID** eingeben (z. B. `651`)
2. **Regex-Vorlage** wählen:
   - **Neu:** `060102_Gesundheit_PersonB` — 6 Ziffern + Unterstrich
   - **Alt NAS:** `060101 Gesundheit PersonA` — 6 Ziffern + Leerzeichen
3. Optional **Quelldokument löschen** ankreuzen — Löschung erst nach erfolgreichem Schreiben **aller** Teile nach `consume/` (Standard: Original bleibt erhalten)
4. **Vorschau** — async (~10–15 s), zeigt eine Tabelle mit Teilen/Seiten/Barcodes; es wird noch nichts geschrieben
5. **Splitten → consume** — nach Bestätigung wandern die Teile nach `PAPERLESS_CONSUME_DIR`, jeder Teil durchläuft die normale Pipeline

Die Statuszeile zeigt den Fortschritt (`PDF laden…` → `QR scannen…` → Ergebnis). Bei einem Hänger: Log `journalctl -u correspondent-manager | grep -i legacy`.

> Ohne Checkbox bleibt das Quelldokument in Paperless unverändert — ggf. manuell archivieren oder taggen. Mit Checkbox wird es nach erfolgreichem Split automatisch gelöscht (die Einstellung wird im Browser gemerkt).

### `.env` auf CT 121

```bash
PAPERLESS_CONSUME_DIR=/mnt/paperless-data/consume
# Quotes Pflicht — ohne Quotes: Scan hängt (Regex kaputt)
LEGACY_SPLIT_QR_REGEX='^[0-9]{6}_[^\s]+$'
```

Einmalig Abhängigkeiten installieren: `sudo ./scripts/ensure-legacy-qr-deps.sh` (ghostscript, zbar, venv).

> **Deploy:** `deploy-to-ct121.sh` führt dieses Skript **automatisch** aus, wenn der Paperless-Container neu erstellt wird (`--force-recreate`). Manuell nur nötig bei erstem Setup oder wenn QR-Split/`pre_consume_qr` «libzbar not found» meldet.

CLI-Diagnose: `legacy_qr_split_test.py` — siehe [`LEGACY_IMPORT.md`](LEGACY_IMPORT.md#qr-split-nachträglich).

---

## 13. Pipeline nachholen

Zeigt Paperless unter **Datei-Aufgaben → Fehlgeschlagen** einen `post_consume`-Fehler, das Dokument ist aber **bereits archiviert** (Roh-Titel, kein Dokument-Review) → Menü **↻ Pipeline**.

1. Paperless-**Dokument-ID** eingeben (z. B. `3606`)
2. **▶ Pipeline starten** — läuft im Hintergrund (Vision + LLM, typisch 1–5 Min)
3. Die Statuszeile pollt automatisch; bei Erfolg erscheinen Tags/Ordner oder ein Eintrag im Dokument-Review
4. Die fehlgeschlagene **Datei-Aufgabe** in Paperless manuell **verwerfen** (das Dokument selbst bleibt erhalten)

Log: `/opt/paperless-scripts/logs/post_consume_v12.log`

> Kein Löschen, kein Rescan — dasselbe Dokument wird lediglich neu klassifiziert.

### Abgrenzung zu ähnlichen Mechanismen

| Mechanismus | Zweck |
|---|---|
| `pre_consume_qr.py` | Swiss **QR-Rechnung** (SPC) beim **neuen** Scan |
| `legacy_split_by_qr.py` | **Metadaten-QR** auf Trennseiten — nachträglich per Dok-ID ([§12](#12-legacy-qr-split)) |
| `legacy-import-batch.sh` | NAS-Bulk ohne OCR/Pipeline (nur Index) |

Details Bulk-Import: [`LEGACY_IMPORT.md`](LEGACY_IMPORT.md)

---

## 14. Dokumente in Paperless finden

Fertige Suchanfragen für die häufigsten Fragen an die Ablage:

| Suchanfrage | Filter |
|---|---|
| Offene Rechnungen | Custom Field `Status` = `Offen` |
| Bezahlte Rechnungen | Custom Field `Status` = `Bezahlt` |
| Zahllauf vom 06.02.2026 | Custom Field `Bezahlt am` = `2026-02-06` |
| Heute gescannt | Custom Field `Gescannt am` = heute |
| Vollautomatisch verarbeitet | Custom Field `Verarbeitung` = `auto STP` |
| Dokumente für Person B | Custom Field `Person` = `PersonB` |
| Steuerbelege 2025 | Tag = `Steuerrelevant` + Datum 2025 |
| Absender X | Korrespondent = «X» |
| Schulberichte mit HTR | Inhalt enthält `--- Handschrift (HTR) ---` |
| Offene HTR-Entscheidung | Tag `pending_htr_decision` |

---

## 15. Fehlerbehebung

### Versionsanzeige stimmt nicht

Die Sidebar zeigt kompakt `UI v… | be v… | pipe v…`, der Start-Tab zusätzlich **pre OCR**, **pre QR** und die Vision/LLM/Embeddings-Modelle.

Stimmt die Anzeige nicht mit dem letzten Deploy überein:

1. Ctrl+Shift+R (Browser Hard-Refresh)
2. `systemctl restart correspondent-manager`

Versionsregeln im Detail: [`VERSIONING.md`](VERSIONING.md).

### Login schlägt fehl (`401 Nicht authentifiziert`)

> **Auth (ab BE 2.35):** API-Calls prüfen die Paperless-Session gegen die **gleiche Basis-URL wie der Browser-Zugriff** — per IP also `http://<IP>:8000`, per Domain die externe URL. Zuvor konnten `PAPERLESS_URL` (Domain) und IP-Zugriff kollidieren.

→ Über denselben Weg (Domain **oder** IP) einloggen, mit dem auch paper.manager aufgerufen wird — nicht mischen.

### PDF-Vorschau fehlt bei IP-Zugriff

Thumbnail und PDF im Dokument-Review laufen über `/api/proxy/document/{id}/thumb/` bzw. `/preview/` — das Backend holt die Datei mit `PAPERLESS_TOKEN` aus der Paperless-API. Ohne diesen Proxy scheitert die Vorschau per IP oft, weil kein Authentik-/Session-Cookie für direkte Paperless-URLs vorliegt. Ist der Proxy erreichbar (Backend läuft, `PAPERLESS_TOKEN` gültig), funktioniert die Vorschau unabhängig vom Zugriffsweg.

### Brillenpass-Übersicht zeigt «Keine Glaswerte»

Obwohl der Review-Vorschlag stimmt (McOptic u. a. speichern in `messung`):

```bash
cd /opt/paperless-ngx-classifier
/opt/paperless-scripts/venv/bin/python3 scripts/repair_brillenpaesse.py
```

Alternativ: Brillenpass-Tab einmal öffnen (Auto-Hydration ab BE 2.59). Nach einer Freigabe werden `messung` und `diagnose.merged` automatisch persistiert.

### Logs und Diagnose im Überblick

| Was | Wo |
|---|---|
| Backend / Service-Fehler | `journalctl -u correspondent-manager` |
| Pipeline-Läufe (`post_consume`) | `/opt/paperless-scripts/logs/post_consume_v12.log` |
| Brillenpass-Fehler | `audit_log.jsonl` + `journalctl -u correspondent-manager` |
| Legacy QR-Split hängt | `journalctl -u correspondent-manager \| grep -i legacy` |
| QR-Split: «libzbar not found» | `sudo ./scripts/ensure-legacy-qr-deps.sh` erneut ausführen |

---

## 16. Schnellreferenz

### Tastaturkürzel

| Taste | Funktion |
|---|---|
| Enter | Tag/Synonym/Keyword hinzufügen |
| Backspace | Letzten Tag löschen (bei leerem Feld) |
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
| 11 | Auto-Kennzeichen | Auswahl | Vision/OCR + `family.json` |
| 12 | Bezahlt am | Datum | Handschrift `bez.` |
| 13 | Gescannt am | Datum | Immer = heute |
| 14 | Verarbeitung | Auswahl | `auto STP` wenn ohne Review fertig |
| 15 | Person | Auswahl | `family.json` bei Kennzeichen-Match oder Beziehung |
