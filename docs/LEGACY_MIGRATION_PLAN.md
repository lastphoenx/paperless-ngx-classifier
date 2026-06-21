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

## Hängende PDFs aufräumen (einmalig nach Bugfix)

**Wichtig:** Skip-Ordner liegt **nicht** mehr unter `consume/` (Paperless scannt rekursiv).

```bash
/opt/paperless-scripts/legacy-migrate-all.sh --cleanup-consume
```

Verschiebt alle PDFs aus `consume/legacy/*` und dem alten `consume/_skipped/` nach  
`/mnt/paperless-data/legacy-migrate/skipped/<batch>/`.

## Notfall: Lauf stoppen + consume leeren

```bash
/opt/paperless-scripts/legacy-migrate-all.sh --stop
/opt/paperless-scripts/legacy-migrate-all.sh --cleanup-consume
/opt/paperless-scripts/legacy-migrate-all.sh --status
```

Migration startet **nicht**, solange PDFs in `consume/legacy` liegen (Preflight).

Chunk-Größe (Standard 25): `LEGACY_CHUNK_SIZE=30` in der Umgebung.

## Migration starten (unbeaufsichtigt)

```bash
nohup /opt/paperless-scripts/legacy-migrate-all.sh >> /mnt/paperless-data/legacy-migrate/nohup.out 2>&1 &
```

Ab bestimmtem Batch (z. B. lohn):

```bash
nohup /opt/paperless-scripts/legacy-migrate-all.sh --from lohn >> /mnt/paperless-data/legacy-migrate/nohup.out 2>&1 &
```

Mit automatischem Nachhol-Versuch am Ende:

```bash
nohup /opt/paperless-scripts/legacy-migrate-all.sh --with-retry >> /mnt/paperless-data/legacy-migrate/nohup.out 2>&1 &
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
| `skipped` | nach `legacy-migrate/skipped/` verschoben (Duplikate/Hänger) |

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
- **Duplikate:** Script setzt temporär `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` — abgelehnte Dateien verschwinden aus consume, der Batch läuft weiter.
- **90 s** ohne Fortschritt + alle Rest-PDFs als Duplikat in Logs → sofort nach `skipped/`.
- **5 Minuten** ohne Fortschritt → Rest nach `legacy-migrate/skipped/<batch>/`, Batch **done**, weiter.
- Kein manuelles Eingreifen nötig.

## Versäumtes nachholen

Nach Abschluss oder bei transienten Fehlern:

```bash
# alle skipped PDFs erneut versuchen
/opt/paperless-scripts/legacy-migrate-all.sh --retry-skipped

# nur einen Batch
/opt/paperless-scripts/legacy-migrate-all.sh --retry-skipped policen
```

Originale bleiben in `skipped/`; Kopien gehen nach `consume/legacy/_retry/`.  
Echte Duplikate (Inhalt schon in Paperless) werden erneut übersprungen — siehe `skipped.tsv` mit Grund `duplicate`.

## Duplikate (Inhalt bereits in Paperless)

Wenn Import abgelehnt wird (`duplicate of #NNN`): Inhalt ist schon da, nur ohne `legacy`-Tag.  
Optionen:

- In der UI dem bestehenden Dokument Tag `legacy` geben, oder
- Eintrag in `skipped.tsv` ignorieren (kein Datenverlust)

## Einzelbatch (manuell)

```bash
/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/BLKB blkb
```

## Nach Abschluss

- `--status`: Summe `legacy_after` vs. NAS-Inventur
- `skipped.tsv`: welche Dateien nicht importiert wurden (meist schon in Paperless)
- NAS-Originale bleiben unverändert unter `Eltern/Finanzen`

## Pfade

| Pfad | Zweck |
|------|--------|
| `consume/legacy/<batch>/` | Import-Queue (Paperless überwacht) |
| `legacy-migrate/skipped/<batch>/` | Übersprungene PDFs (**außerhalb** consume) |
| `legacy-migrate/skipped.tsv` | Protokoll mit Grund |
| `legacy-migrate/state.tsv` | Batch-Fortschritt |
| ~~`consume/_skipped/`~~ | **Veraltet** — per `--cleanup-consume` migrieren |
