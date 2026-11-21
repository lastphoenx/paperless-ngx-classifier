# Paperless-NGX — Gespeicherte Ansichten, Dashboard & 3.5.x

**Produktion CT 121:** Paperless **3.5.1** (Image-Pin, Nov 2026).  
**Classifier:** API weiterhin `Accept: application/json; version=9` (kompatibel).

Ausführliche UI-Anpassungen: [PAPERLESS_CUSTOMIZATION.md](./PAPERLESS_CUSTOMIZATION.md) · Upgrade-Historie: [UPGRADE_V3.md](./UPGRADE_V3.md)

---

## Kurz — wie Ansichten und Widgets zusammenhängen

| Begriff | Bedeutung |
|---------|-----------|
| **Gespeicherte Ansicht** | Gespeicherter Filter der Dokumentenliste inkl. Sortierung und Darstellung (Tabelle/Karten, Spalten, Seitengrösse). |
| **Seitenleiste** | Optionaler Eintrag pro Ansicht — mit Dokumentanzahl, wenn in den UI-Einstellungen aktiviert. |
| **Dashboard-Widget** | Dieselbe Ansicht als **Dokumentenliste** auf dem Dashboard (keine Summen/KPIs). |
| **Aktualisierung** | Widgets/Listen aktualisieren sich selbst; das Dashboard zeigt ansonsten keine «Live-Dokumente» ausser über diese Widgets. |

**Seit API v10:** «Show on dashboard» und «Show in sidebar» liegen **nicht** mehr an der Ansicht selbst, sondern **pro Benutzer** unter **Einstellungen → Darstellung / Saved views** (bzw. engl. *Settings → Display*). Die Ansicht speichern, dann dort einblenden.

**Teilen (ab 3.0):** Berechtigungen an der Ansicht (Benutzer/Gruppen). Dashboard und Seitenleiste richtet **jeder Nutzer für sich** ein.

---

## Schritt-für-Schritt — erste Ansicht anlegen

1. **Dokumente** öffnen und Filter setzen (Tags, Korrespondent, **Benutzerdefinierte Felder**, Volltext, Duplikate ab 3.2, …).
2. Optional **Anzeige** (Listen-Icon): Tabellen/Karten, Spalten inkl. Custom Fields, Symbol für die Ansicht (ab 3.1).
3. **Aktuelle Ansicht speichern** → Namen vergeben (z. B. `Offene Rechnungen`).
4. **Einstellungen → Darstellung** → bei der Ansicht **Seitenleiste** und/oder **Dashboard** einschalten.
5. **Dashboard:** Welcome-Widget bei Bedarf mit **X** entfernen; gespeicherte Ansichten erscheinen als Listen-Widgets.
6. **Teilen:** Ansicht öffnen → Berechtigungen → Partner/in oder Gruppe lesen (oder Bearbeiten).

Dokumentation: [Saved views](https://docs.paperless-ngx.com/usage/#saved-views), [API saved views](https://docs.paperless-ngx.com/api/#saved-views).

---

## Rezeptbuch — passend zu eurer Pipeline (Custom Fields)

IDs entsprechen `INSTALL.md` / `.env.example` (`CF_*_ID` auf CT 121):

| ID | Feld | Typ |
|----|------|-----|
| 1 | CHF (Betrag) | Monetär |
| 8 | Fällig am | Datum |
| 9 | Status | Auswahl |
| 12 | Bezahlt am | Datum |
| 15 | Person | Auswahl |
| 17 | Steuerjahr | Zahl/Text (von `post_consume` / `steuerjahr.py`) |

### Steuer 2026

- Tag **Steuerrelevant** (oder euer Steuer-Dokumenttyp).
- Custom Field **Steuerjahr** = `2026` (besser als reiner Datumsfilter: Pipeline leitet das Jahr aus dem Beleg ab, z. B. 3a-Bescheinigung März 2027 → Steuerjahr 2026).
- Zusätzliche Ansichten: gleicher Filter + **Person** = jeweilige Person aus `family.json`.

### Säule 3a

- Tag **Säule 3a** oder Dokumenttyp «Säule 3a».
- Optional **Steuerjahr** als Spalte/Filter.

### Offene Rechnungen

- **Fällig am:** gesetzt (existiert).
- **Bezahlt am:** nicht gesetzt — in der Oberfläche unter Benutzerdefinierte Felder «existiert nicht» / leer; API: Feld fehlt im Filter.
- Ergänzen: **Status** = `Offen`, falls die Pipeline den Wert setzt.

Alternativ zur manuellen Suche: Tabelle in [Benutzerhandbuch §14](./Benutzerhandbuch_paper_manager.md#14-gespeicherte-ansichten-und-suche).

### Fällig in 14 Tagen

- **Fällig am** mit relativem Bereich, API-Beispiel: `["Fällig am","range",["today","in 14 days"]]`.
- Seit **3.4.0** in Workflows/Filtern für Custom Fields; die globale Volltextsuche unterstützt relative Datumsausdrücke weiterhin **nicht** — nur strukturierte Filter testen (Custom-Field-Dialog). Falls die UI es nicht anbietet: später paper.manager-Widget oder API-Filter.

### Empfohlene Dashboard-Kacheln (Start)

| Ansicht | Nutzen |
|---------|--------|
| Offene Rechnungen | Zahllauf ohne paper.manager öffnen |
| Fällig in 14 Tagen | Kurzfristige Zahlungen |
| Steuer 2026 · Person A/B | Steuerordner mental entlasten |
| Needs attention / Tasks | v3 Tasks-UI — offene Asynchron-Jobs |

paper.manager bleibt Ort für **Review**, Korrespondenten und Brillenpass — Paperless-Dashboard für **fertige** Dokumentlisten.

---

## Changelog 3.x — was für uns relevant ist

| Version | Thema | Aktion bei uns |
|---------|--------|----------------|
| **3.0** | Ansichten teilen; Tasks-UI | Berechtigungen auf Shared Views; «Datei-Aufgaben» / Needs attention kennen |
| **3.1** | Symbole für Ansichten; Welcome-Widget überarbeitet | Sidebar-Icons pflegen |
| **3.2** | Seitenleisten-Einträge ausblendbar; **Duplikat-Filter** | Legacy-Dubletten prüfen |
| **3.3** | **Dokumentversionen**; Notizen; Barcodes speichern/suchen (Upstream); Django-Admin 2FA | Versionen für korrigierte PDFs; Barcode-Suche evaluieren (siehe unten) |
| **3.4** | Relative Datumsabfragen (Custom Fields); Workflows + CF in Titelvorlagen; 2FA-Skip bei vertrauenswürdigem OIDC; **poppler** statt ImageMagick für Vorschau | Fällig-Filter; Workflows; Authentik-Login unverändert nutzbar |
| **3.5** | **Widget-Queue** für Saved Views (schneller/stabiler); Notizen **Markdown**; manueller **Workflow-Auslöser**; Bulk async; Session-Cleanup | Dashboard nach Update testen; Workflows manuell testen |

KI (Chat, Suggestions, eigener Embedding-Key): nur relevant, wenn aktiv — wir nutzen Embeddings (`bge-m3`), Generation-LLM bewusst leer ([UPGRADE_V3.md](./UPGRADE_V3.md) §3.2.1).

---

## Barcodes (Upstream vs. QR-Pipeline)

| Komponente | Rolle |
|------------|--------|
| **`PAPERLESS_CONSUMER_ENABLE_BARCODES=true`** | Paperless erkennt Barcodes beim Consume (zxing-cpp). **Auf CT 121 gesetzt.** |
| **`PAPERLESS_CONSUMER_STORE_BARCODE_VALUES`** | Standard **aus**. Wenn nicht gesetzt: erkannte Barcodes werden nicht dauerhaft gespeichert — Suche `barcodes:` und Metadaten-Tab nutzen das nicht. |
| **`pre_consume_qr.py` / Legacy QR-Split** | Swiss QR-Rechnung + **Trennseiten-QR** — unabhängig vom Upstream-Barcode-Store. |

**Optional profitieren (3.3+):** `PAPERLESS_CONSUMER_STORE_BARCODE_VALUES=true` in `/opt/paperless/.env`, dann `docker compose up -d --force-recreate webserver`. Suche: `barcodes:<inhalt>`. Bestehende Dokumente: **Reprocessing** nötig. Ergänzt die QR-Trennseiten-Pipeline (`pre_consume_qr` / Legacy-Split), ersetzt sie nicht.

---

## Workflows (3.4 / 3.5)

- Custom Fields in **Titelvorlagen** und Filter mit **relativen Datumsangaben**.
- **Manueller Auslöser** (3.5): Workflow auf Dokument → manuell starten (gut zum Testen vor Auto-Trigger).
- Eltern- und Kind-Tags unabhängig setzbar.

**Vorsicht:** Doppelte Logik zu `post_consume.py` vermeiden. Workflows eignen sich für **Nachgelagertes** (z. B. Tag setzen wenn Fällig abgelaufen), nicht zum Ersetzen der Klassifizierungs-Pipeline.

---

## Weitere 3.5.x-Features im Alltag

| Feature | Nutzen |
|---------|--------|
| **Dokumentversionen** | Neue Scan-Version zu bestehendem Dokument (Korrektur-PDF ohne Dubletten-Chaos). |
| **Notizen mit Markdown** | Prüfvermerke, Links, Checklisten im Dokument-Detail. |
| **Bulk async** | Grosse Auswahl (Tags löschen, …) blockiert UI kürzer — nach Bulk «Tasks» prüfen. |
| **Poppler-Vorschau** | Weniger ImageMagick-Abhängigkeit — kein Betreiber-Tuning nötig. |

---

## Upgrade 3.1.2 → 3.5.1 (Kurz)

Bereits auf v3-Migration:

```bash
cd /opt/paperless
cp docker-compose.yml docker-compose.yml.bak-pre351
# image: ghcr.io/paperless-ngx/paperless-ngx:3.5.1
docker compose pull webserver
docker compose up -d --force-recreate webserver
docker compose logs -f webserver   # bis «ready»
cd /opt/paperless-ngx-classifier && ./scripts/paperless-version-check.sh
```

Kein Downgrade ohne PBS-Snapshot. Release Notes: [GitHub Releases](https://github.com/paperless-ngx/paperless-ngx/releases).

---

## Fehler: «Save view» / «Edit Permissions» → 403 auf `/api/users/`

**Symptom:** Dialog «Save view» — beim Aufklappen von **Edit Permissions** rote Meldung *Error retrieving users*, HTTP **403** auf  
`GET /api/users/?page=1&page_size=100000`.

**Ursache (Paperless-Berechtigungen, nicht Authentik):** Das Freigabe-UI lädt die Benutzerliste. Dafür braucht der eingeloggte Account die **globale** Django-Permission **User → View** (`view_user` → `/api/users/`).  
SavedView/UISettings allein reichen nicht. Siehe [usage.md — Global Permissions «User»](https://docs.paperless-ngx.com/usage/#global-permissions).

### Sofort-Workaround

Ansicht **speichern ohne** «Edit Permissions» aufzuklappen — Owner bleibt du, Sidebar/Dashboard unter **Einstellungen → Darstellung**. Kein Teilen mit anderen Nutzern, aber Dashboard funktioniert.

### Dauerfix (Haushalt «Eltern»)

Einmal als **Superuser** (`paperlessngx-santo`) oder Admin mit Zugriff auf **Einstellungen → Users & Groups**:

1. Gruppe **Eltern** (oder betroffenen User) bearbeiten → **Global permissions** → **User: View** (optional **Group: View**, falls Gruppen-Dropdown auch 403 wirft).
2. Gespeicherte Ansichten: mindestens **View** + **Add** + **Change** auf **SavedView** (falls noch fehlend).

**CT 121 — Django-Shell (Gruppe Eltern):**

```bash
docker compose -f /opt/paperless/docker-compose.yml exec -T webserver python3 manage.py shell -c "
from django.contrib.auth.models import Group, Permission
g = Group.objects.get(name='Eltern')
for label, codename in [
    ('auth', 'view_user'),
    ('auth', 'view_group'),
]:
    p = Permission.objects.get(content_type__app_label=label, codename=codename)
    g.permissions.add(p)
print('ok', list(g.permissions.filter(content_type__app_label='auth').values_list('codename', flat=True)))
"
```

Danach **neu einloggen** (oder Session-Refresh). Privat mehr Kontext: `doku/pve2/vm/121-paperless/authentik-oidc.md`.

**Sicherheit:** `view_user` erlaubt die Benutzerliste in den Einstellungen — für einen Zwei-Personen-Haushalt üblich; nicht für «nur Lesen ohne Namen von Konten».

---

## Checkliste nach Einrichtung

- [ ] Mindestens eine Shared View «Offene Rechnungen» mit Rechte für Haushalt
- [ ] Dashboard pro Benutzer: Sidebar + 1–2 Widgets
- [ ] Steuerjahr-Spalte in Steuer-Ansichten sichtbar
- [ ] `paperless-version-check.sh` → Pin **3.5.1**
- [ ] Optional: Barcode-Store + ein Test-PDF
- [ ] Optional: ein Workflow nur für manuellen Test (kein Konflikt mit post_consume)