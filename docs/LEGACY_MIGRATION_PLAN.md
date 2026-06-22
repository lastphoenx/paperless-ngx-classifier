# Legacy-Migration — Eltern/Finanzen (CT 121)

**Ziel:** NAS-PDFs → Paperless mit Tag `legacy`, Speicherpfad `legacy/{title}`, OCR-Index (`PAPERLESS_OCR_MODE=skip`), **ohne** Vision/LLM-Pipeline.

Quelle: `/mnt/nas-legacy/Eltern/Finanzen` · ~2561 PDFs (ohne `Vorsorge/Moni/2015` und `2016`)

---

## Empfohlen: ein Batch, Vordergrund (`legacy-one-batch.sh`)

**Nicht** `nohup`, **nicht** `/root/legacy-migrate-resume.sh`, **nicht** `legacy-migrate-all.sh` (zu fehleranfällig).

### Einmalig `.env` (`/opt/paperless/.env`)

```bash
LEGACY_CONSUME_MARKERS=/legacy/
LEGACY_TAG=legacy
LEGACY_SET_BATCH_TAG=false
LEGACY_STORAGE_PATH_TEMPLATE=legacy/{title}
PAPERLESS_OCR_MODE=skip
PAPERLESS_CONSUMER_DELETE_DUPLICATES=true   # Duplikate aus consume entfernen
PAPERLESS_TASK_WORKERS=1
```

```bash
cd /opt/paperless-ngx-classifier && git pull && ./scripts/deploy-to-ct121.sh
```

### Alten Parallel-Lauf beenden (wichtig)

```bash
pgrep -af 'legacy-migrate-resume|legacy-migrate-all'
# falls /root/legacy-migrate-resume.sh läuft:
kill $(pgrep -f legacy-migrate-resume) 2>/dev/null || true
mv /root/legacy-migrate-resume.sh /root/legacy-migrate-resume.sh.DISABLED 2>/dev/null || true
```

### Status (inkl. Dateiaufgaben „Fehlgeschlagen“)

```bash
/opt/paperless-scripts/legacy-tasks-summary.sh
/opt/paperless-scripts/legacy-duplicate-audit.sh   # Tasks vs. einzigartige Duplikat-Dateien
/opt/paperless-scripts/legacy-nas-sha256.sh all      # NAS SHA256-Inventar + erwartete Dubletten
```

Zeigt dieselbe Zahl wie die UI: Fehlgeschlagen / Warteschlange / legacy-Tag / consume.

### Nur fehlende Inhalte importieren (empfohlen)

Einmalig Delta bauen, dann **ein Befehl** in tmux:

```bash
/opt/paperless-scripts/legacy-nas-sha256.sh missing   # einmalig (~945 Einträge)

tmux new -s legacy
/opt/paperless-scripts/legacy-nas-sha256.sh import-loop --batch queue --chunk 20
```

Pro Chunk automatisch:
1. **Pop** N Zeilen aus `missing.tsv` → `in-flight.tsv` + kopieren
2. Wartet bis `consume` leer
3. **Reconcile** — nur was Paperless bestätigt ist raus; Rest zurück in `missing.tsv`

Kein `done.lst`, kein „kopiert = erledigt“.

### Pro NAS-Ordner (in **tmux**)

```bash
tmux new -s legacy
/opt/paperless-scripts/legacy-one-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Fano fano
# nächster Ordner wenn fertig:
/opt/paperless-scripts/legacy-one-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Bestellungen bestellungen
```

Chunk-Größe: `LEGACY_CHUNK_SIZE=20` (Standard). Script wartet zwischen Chunks, bis consume leer ist.

### Erledigte Batches (Stand manuell pflegen)

| Slug | NAS-Ordner | Anmerkung |
|------|------------|-----------|
| cs | CS | done — meist Duplikate in skipped |
| policen | Policen | done |
| lohn | Lohn | done — 90 PDFs |
| fano | Fano | **als Nächstes** |
| bestellungen | Bestellungen | offen |
| blkb | BLKB | offen |
| ameritrade | Ameritrade | offen |
| erb-bern | Erb_Bern | offen |
| erbschaft-gassacker | Erbschaft_Gassacker | offen |
| steuern | Steuern | offen |
| rechnungen | Rechnungen | offen |
| vorsorge-* | Vorsorge/… | offen |

---

## „197 Fehlgeschlagen“ in der UI

Das sind **keine kaputten Imports** im Sinne von Datenverlust. Typisch:

```
Not consuming … It is a duplicate of … (#NNN)
```

→ Inhalt **ist schon in Paperless** (ohne `legacy`-Tag). Paperless protokolliert das als fehlgeschlagene Dateiaufgabe.

| Was tun | Warum |
|---------|--------|
| `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` | Datei verschwindet aus consume, Queue läuft weiter |
| UI: **Alle verwerfen** | Kosmetik — Tasks aus der Liste |
| Optional: bestehendem Doc Tag `legacy` geben | Kein Re-Import nötig |

`legacy-migrate-all.sh` hat die Task-API **nie** abgefragt — dafür gibt es jetzt `legacy-tasks-summary.sh`.

---

## Notfall: consume voll, unklar wer nachlegt

```bash
/opt/paperless-scripts/legacy-tasks-summary.sh
pgrep -af legacy
for d in /mnt/paperless-data/consume/legacy/*/; do
  echo "$(basename "$d"): $(find "$d" -name '*.pdf' | wc -l)"
done
```

Nur `legacy-migrate-resume.sh` beenden — consume **nicht** leeren, wenn Paperless noch arbeitet (Zahl sinkt).

---

## Veraltet

- `legacy-migrate-all.sh` + `nohup` — nur noch mit Vorsicht
- `/root/legacy-migrate-resume.sh` — deaktivieren
- `consume/_skipped/` — nach `legacy-migrate/skipped/` migrieren

Details Pipeline: [LEGACY_IMPORT.md](./LEGACY_IMPORT.md)
