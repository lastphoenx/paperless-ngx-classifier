# Legacy-Migration — Eltern/Finanzen (CT 121)

Stand: Juni 2026 · Quelle: `/mnt/nas-legacy/Eltern/Finanzen` · ~2322 PDFs (ohne `Vorsorge/Moni/2015` und `2016`)

**Orchestrierung:** `scripts/legacy-migrate-all.sh` — nicht manuell warten/skippen.

## Einmalig vorbereiten

```bash
cd /opt/paperless-ngx-classifier && git pull && ./scripts/deploy-to-ct121.sh
# Pipe 12.32 + LEGACY_* in /opt/paperless/.env — siehe LEGACY_IMPORT.md
```

Alte kaputte Scripts auf CT121 stoppen:

```bash
pkill -f legacy-migrate-all.sh || true
pkill -f legacy-migrate-resume.sh || true
```

Bereits erledigte Batches markieren (z. B. nach manuellem CS/Policen-Lauf):

```bash
/opt/paperless-scripts/legacy-migrate-all.sh --mark-done cs,policen
```

Hängende PDFs in consume (Duplikate) — einmalig wegräumen:

```bash
mkdir -p /mnt/paperless-data/consume/_skipped/policen
find /mnt/paperless-data/consume/legacy/policen -type f -iname '*.pdf' \
  -exec mv -t /mnt/paperless-data/consume/_skipped/policen/ {} + 2>/dev/null || true
rm -rf /mnt/paperless-data/consume/legacy/policen
```

## Migration starten (unbeaufsichtigt)

```bash
nohup /opt/paperless-scripts/legacy-migrate-all.sh >> /mnt/paperless-data/legacy-migrate/nohup.out 2>&1 &
```

Ab bestimmtem Batch (z. B. lohn):

```bash
nohup /opt/paperless-scripts/legacy-migrate-all.sh --from lohn >> /mnt/paperless-data/legacy-migrate/nohup.out 2>&1 &
```

## Überblick — jederzeit

```bash
/opt/paperless-scripts/legacy-migrate-all.sh --status
```

Zeigt Tabelle pro Batch:

| Spalte | Bedeutung |
|--------|-----------|
| `expected` | PDFs auf NAS in diesem Ordner |
| `legacy_before` / `legacy_after` | Paperless-Tag `legacy` vor/nach Batch |
| `skipped` | nach `_skipped` verschoben (Duplikate/Hänger) |

Details jeder übersprungenen Datei:

```bash
column -t -s $'\t' /mnt/paperless-data/legacy-migrate/skipped.tsv
```

Vollständiges Log:

```bash
tail -f /mnt/paperless-data/legacy-migrate/migrate.log
```

## Automatik bei Hängern

- Wartet auf **PDF-Dateien** (nicht leere Unterordner).
- **5 Minuten** ohne Fortschritt → Rest nach `consume/_skipped/<batch>/`, Batch wird **done** markiert, weiter mit nächstem Ordner.
- Kein manuelles Eingreifen nötig.

## Einzelbatch (manuell)

```bash
/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/BLKB blkb
```

## Nach Abschluss

- `--status`: Summe `legacy_after` vs. NAS-Inventur
- `skipped.tsv`: welche Dateien nicht importiert wurden (meist schon in Paperless)
- NAS-Originale bleiben unverändert unter `Eltern/Finanzen`
