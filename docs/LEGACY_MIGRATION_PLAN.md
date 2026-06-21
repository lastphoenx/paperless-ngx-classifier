# Legacy-Migration — Eltern/Finanzen (CT 121)

Stand: Juni 2026 · Quelle: `/mnt/nas-legacy/Eltern/Finanzen` · ~2322 PDFs (ohne `Vorsorge/Moni/2015` und `2016`)

Geschätzte Dauer: **~6 s/PDF** → große Batches mehrere Stunden.

## Vor dem Start

```bash
# 1) Pipe 12.32 + .env (LEGACY_SET_BATCH_TAG=false, LEGACY_STORAGE_PATH_*)
cd /opt/paperless-ngx-classifier && git pull && ./scripts/deploy-to-ct121.sh

# 2) Alte Test-Dokumente in Paperless UI löschen (legacy-moni-2015-test, blkb-smoke, none/none-Tests)

# 3) Inventur (optional)
find /mnt/nas-legacy/Eltern/Finanzen -name '*.pdf' \
  ! -path '*/Vorsorge/Moni/2015/*' \
  ! -path '*/Vorsorge/Moni/2016/*' | wc -l

# PDFs pro Top-Level-Ordner
for d in /mnt/nas-legacy/Eltern/Finanzen/*/; do
  printf '%5d  %s\n' "$(find "$d" -name '*.pdf' | wc -l)" "$(basename "$d")"
done | sort -rn
```

## Warten bis Batch fertig

```bash
# Wiederholen bis leer (oder watch):
watch -n 30 'ls /mnt/paperless-data/consume/legacy/BATCHNAME/ 2>/dev/null | wc -l'

# Oder einmalig:
test -z "$(ls -A /mnt/paperless-data/consume/legacy/BATCHNAME/ 2>/dev/null)" && echo FERTIG
```

Nächsten Batch **erst** starten, wenn der vorherige Consume-Ordner leer ist (`PAPERLESS_TASK_WORKERS=1`).

## Migrations-Reihenfolge

Klein → groß. Jede Zeile = ein Befehl, nacheinander ausführen.

### Phase 1 — kleine Ordner (~30–60 Min gesamt)

```bash
/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/CS cs
# warten …

/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Policen policen
# warten …

/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Lohn lohn
# warten …

/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Fano fano
# warten …

/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Bestellungen bestellungen
# warten …
```

### Phase 2 — mittel (~25–45 Min pro Batch)

```bash
/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/BLKB blkb
# ~242 PDFs · warten …

/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Ameritrade ameritrade
# warten …

/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Erb_Bern erb-bern
# warten …

/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Erbschaft_Gassacker erbschaft-gassacker
# warten …
```

### Phase 3 — Vorsorge (Moni 2015/2016 auslassen)

Bereits in Paperless oder Test — **nicht** nochmals importieren.

```bash
# Nur Unterordner einzeln, NICHT Moni/2015 und Moni/2016:
for sub in /mnt/nas-legacy/Eltern/Finanzen/Vorsorge/*/; do
  base=$(basename "$sub")
  [[ "$base" == "Moni" ]] && continue
  echo "=== $base ===" && find "$sub" -name '*.pdf' | wc -l
done

# Beispiel einzelner Unterordner (Namen anpassen nach Inventur):
# /opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Vorsorge/<name> vorsorge-<name>
```

Für `Vorsorge/Moni`: nur Jahre **≠** 2015 und 2016:

```bash
for y in /mnt/nas-legacy/Eltern/Finanzen/Vorsorge/Moni/*/; do
  base=$(basename "$y")
  [[ "$base" == "2015" || "$base" == "2016" ]] && continue
  /opt/paperless-scripts/legacy-import-batch.sh "$y" "vorsorge-moni-$base"
  # warten bis consume leer …
done
```

### Phase 4 — große Batches (abends / nachts)

```bash
/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Steuern steuern
# ~800 PDFs · ~1.5 h · warten …

/opt/paperless-scripts/legacy-import-batch.sh /mnt/nas-legacy/Eltern/Finanzen/Rechnungen rechnungen
# ~800 PDFs · ~1.5 h · warten …
```

Weitere Top-Level-Ordner aus der Inventur (`Familien_*`, `Jörg*`, …) analog:

```bash
/opt/paperless-scripts/legacy-import-batch.sh "/mnt/nas-legacy/Eltern/Finanzen/ORDNER" batch-slug
```

### Phase 5 — Einzel-PDFs im Wurzelverzeichnis

```bash
mkdir -p /mnt/paperless-data/consume/legacy/finanzen-root
find /mnt/nas-legacy/Eltern/Finanzen -maxdepth 1 -name '*.pdf' -exec cp -n {} /mnt/paperless-data/consume/legacy/finanzen-root/ \;
# warten …
```

## Stichprobe nach jedem großen Batch

```bash
# Anzahl legacy-Doks in API
TOKEN=$(grep '^PAPERLESS_TOKEN=' /opt/paperless/.env | cut -d= -f2-)
curl -s -H "Authorization: Token $TOKEN" \
  'http://127.0.0.1:8000/api/documents/?tags__id__all=TAG_ID_LEGACY&page_size=1' | \
  python3 -c "import sys,json; print(json.load(sys.stdin).get('count'))"

# Physisch auf ssd1
find /mnt/ssd1/Paperless/media/documents/originals/legacy -type f | wc -l
```

## Nach Abschluss

- UI: Filter `Tag: legacy` — Stichproben öffnen (PDF, Speicherpfad `legacy/...`)
- NAS-Originale unter `Eltern/Finanzen` **bleiben** (nur Kopien importiert)
- Optional: `rm -rf /mnt/ssd2/Paperless.mergerfs-alt-*` auf pi-nas nach finaler Prüfung
