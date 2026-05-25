# paper.manager — Benutzerhandbuch

**Version 1.3 | Mai 2026**

---

## 1. Einführung

paper.manager ist die Review- und Verwaltungsoberfläche für die automatische Dokumentklassifizierung mit Paperless-NGX. Nach jedem Scan analysiert ein lokales KI-System (Vision + LLM) das Dokument vollständig als Bild — erkennt Absender, Datum, Betrag, Kennzeichen und handschriftliche Notizen.

> 💡 **Tipp:** Bezahlte Rechnungen mit `bez. 6.2.26` oben rechts markieren — das System setzt automatisch `Status=Bezahlt` und `Bezahlt am=06.02.2026`.

Klick auf **«paper.manager»** im Logo öffnet die Landing Page mit vollständiger Systemübersicht.

### Zugangswege

| Zugang | URL | Auth |
|---|---|---|
| Via Domain (empfohlen) | https://paperless.example.com/corr-manager/ | Authentik SSO |
| Via interne IP | http://ipadresse_paperless_server:8100 | Paperless lokaler Login |

### Versionsanzeige

Direkt unter dem Logo zeigt die Sidebar die aktuellen Versionen:
```
UI v2.2 | be v2.2 | pipe v12.3
```
Stimmt die Version nicht → Ctrl+Shift+R oder Service-Restart.

### Navigation

| Menüpunkt | Hash | Funktion |
|---|---|---|
| Logo-Klick | `#home` | Landing Page — Systemübersicht |
| ! Korrespondenten Review | `#pending` | Neue Absender freigeben |
| # Korrespondenten | `#correspondents` | Absender verwalten |
| D Dokument-Review | `#docreview` | Unsichere Dokumente prüfen |
| T Dokumenttypen | `#doctypes` | Synonyme + Ausschluss-Keywords |
| ~ Tags | `#tags` | Tags + Ausschluss-Keywords |
| M Manifest | `#manifest` | Ordner konfigurieren |
| 👪 Familie | `#family` | Haushalt, Personen, Fahrzeuge |

Alle Tabs haben ein **Suchfeld** für live Filterung.

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
| Standard-Dokumenttyp | Typischer Dokumenttyp dieses Absenders |
| Varianten | Alternative Schreibweisen (Enter / × ) |
| Match-Strings | Suchbegriffe für Paperless-Matching |
| Typische Ordner | Format: Hauptordner/Unterordner |
| Notiz | Interne Bemerkung |

### Aktionen
- **Freigeben** → Korrespondent in Paperless + correspondents.json, neue Ordner als PENDING im Manifest
- **Ablehnen** → Eintrag verworfen
- **⇔ Merge** → Duplikate zusammenführen vor Freigabe

> ⚠️ Tags werden nicht auf Korrespondenten-Ebene gepflegt.

---

## 5. Korrespondenten verwalten

Edit-Button beim Eintrag. Felder: Standard-Dokumenttyp, Varianten, Match-Strings, Typische Ordner, Notiz.

**Merge:** ⇔ Mit anderem zusammenführen → alle Dokumente umgeschrieben, Duplikat gelöscht.

Suchfeld filtert nach Name, Varianten, Match-Strings, Ordnern, Notiz.

---

## 6. Dokument-Review

| Tag | Farbe | Bedeutung |
|---|---|---|
| `pending_review` | gelb | KI unsicher, Datum verdächtig, Fallback-Ordner |
| `pending_qs` | grün | QS-Modus — alle Dokumente prüfen |
| `pending_new_correspondent` | rot | Unbekannter Absender |

### Aktionen
- **✓ Freigeben** → pending-Tags entfernt
- **✎ Neu klassifizieren** → Ordner/Tags/Korrespondent anpassen → Paperless + Manifest lernen
- **✗ Ignorieren** → aus Queue entfernt

---

## 7. Dokumenttypen

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

> Zuerst Personen speichern — dann können Fahrzeuge erfasst werden.
> Person kann nicht gelöscht werden solange Fahrzeuge damit verknüpft sind.

### Bereich 2: Fahrzeuge

Kennzeichen ermöglichen **deterministisches Routing** ohne LLM-Aufruf:
Vision erkennt Kennzeichen → Dokument direkt in konfigurierten Ziel-Ordner.
Schneller und zuverlässiger als LLM-Klassifizierung.

- Kennzeichen muss eindeutig sein
- Person muss aus gespeicherten Personen gewählt werden
- Ziel-Ordner im Format `Person/Kategorie`

---

## 11. Scan-Workflow

1. Dokumente mit Trennseiten auf Scanner legen
2. Scan starten (Profil «paperless»)
3. ~1–2 Minuten pro Dokument warten
4. paper.manager öffnen — rote Badges zeigen offene Einträge
5. **Korrespondenten Review** → freigeben oder ablehnen
6. **Dokument-Review** → bestätigen oder korrigieren
7. **Manifest** → neue pending-Ordner ergänzen

---

## 12. Dokumente in Paperless finden

| Suchanfrage | Filter |
|---|---|
| Offene Rechnungen | Custom Field `Status` = `Offen` |
| Bezahlte Rechnungen | Custom Field `Status` = `Bezahlt` |
| Zahllauf vom 06.02.2026 | Custom Field `Bezahlt am` = `2026-02-06` |
| Heute gescannt | Custom Field `Gescannt am` = heute |
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
| 11 | Auto-Kennzeichen | Auswahl | Vision + family.json |
| 12 | Bezahlt am | Datum | Handschrift bez. |
| 13 | Gescannt am | Datum | Immer = heute |
