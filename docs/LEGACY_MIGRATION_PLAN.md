# Legacy-Migration — Eltern/Finanzen (CT 121)

**Ziel:** NAS-PDFs → Paperless mit Tag `legacy`, Speicherpfad `legacy/{title}`, OCR-Index (`PAPERLESS_OCR_MODE=skip`), **ohne** Vision/LLM-Pipeline.

**Quelle:** Pfad auf CT 121 zum Ordner `Finanzen` — abhängig vom NFS-Mount (siehe [NAS_NFS_AND_IMPORT.md](./NAS_NFS_AND_IMPORT.md)). Typisch bei Export `:/mnt/ssd1/Eltern`: **`/mnt/<eltern-mount>/Finanzen`**, nicht zwingend `/mnt/nas-legacy/...`.

**Stand Finanzen:** Migration abgeschlossen (`missing` = 0 einzigartige fehlende, ~2199 Docs in Paperless).

> **Paperless-Version:** Produktion läuft **2.20.15** (compose gepinnt). Legacy **vor** Upgrade auf v3 abschliessen — siehe [UPGRADE_V3.md](./UPGRADE_V3.md).

---

## Finanzen importieren (Checksum-Delta) — Referenz

**Ein Befehl** in tmux — nicht `copy-missing` manuell wiederholen:

```bash
export LEGACY_NAS_FINANZEN=/mnt/<eltern-mount>/Finanzen   # echten Pfad aus: findmnt | grep 141.140
export LEGACY_MIGRATE_STATE_DIR=/mnt/paperless-data/legacy-migrate

/opt/paperless-scripts/legacy-nas-sha256.sh scan
/opt/paperless-scripts/legacy-nas-sha256.sh fetch-paperless --refresh-paperless
/opt/paperless-scripts/legacy-nas-sha256.sh missing

tmux new -s legacy
/opt/paperless-scripts/legacy-nas-sha256.sh import-loop --batch queue --chunk 20
```

### State-Dateien (`$LEGACY_MIGRATE_STATE_DIR`)

| Datei | Bedeutung |
|-------|-----------|
| `nas-sha256.tsv` | NAS-Inventar |
| `paperless-checksums.tsv` | Paperless MD5-Cache |
| `nas-missing-import.tsv` | Queue: fehlt noch in Paperless |
| `nas-in-flight.tsv` | Pro Chunk unterwegs; reconcile bestätigt Import |

Pro Chunk: pop → consume → warten → **reconcile** (live Delta). Kein `done.lst`.

Details NFS + Thomas/Monika: [NAS_NFS_AND_IMPORT.md](./NAS_NFS_AND_IMPORT.md)

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

### Nur fehlende Inhalte importieren

Siehe Abschnitt **Finanzen importieren** oben. Veraltet: manuelles `copy-missing`, `nas-missing-done.lst`.

### Pro NAS-Ordner (Alternative: `legacy-one-batch.sh`)

Pfad `$NAS_SRC` = echter Mount + Unterordner (aus `findmnt` auf CT 121):

```bash
tmux new -s legacy
/opt/paperless-scripts/legacy-one-batch.sh "$NAS_SRC/Fano" fano
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
