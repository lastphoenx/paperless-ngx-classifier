# NAS → Paperless: NFS (Ist) und Import-Pfade

Stand: Juni 2026 · CT 121 + pi-nas `192.168.141.140`

Dieses Dokument beschreibt den **tatsächlichen** NFS-Stand und wie Import-Pfade ermittelt werden — nicht den älteren Soll-Entwurf mit `:/srv/nas` → `/mnt/nas-legacy`, falls der bei euch nie so eingespielt wurde.

Siehe auch: [ct121-nfs-fix.md](../../../doku/pve2/vm/121-paperless/Doku/docs/ct121-nfs-fix.md), [LEGACY_MIGRATION_PLAN.md](./LEGACY_MIGRATION_PLAN.md)

---

## 1. NFS Ist-Zustand

### pi-nas `192.168.141.140` — `/etc/exports` (Stand prüfen)

Typisch dokumentiert:

```text
/mnt/ssd1/Paperless/media  192.168.131.31(rw,...,all_squash,anonuid=2003,anongid=2003,fsid=1)
/mnt/ssd1/Eltern             192.168.131.31(ro,...,no_root_squash,fsid=2)
```

**Hinweis:** Wenn CT 121 `:/mnt/ssd1` gemountet hat (siehe unten), muss auf pi-nas auch **`/mnt/ssd1`** (oder ein gleichwertiger Export) für `.31` freigegeben sein — sonst widersprechen exports und fstab. Nach Änderung: `exportfs -rav`.

### CT 121 — `/etc/fstab` (verifiziert)

```fstab
192.168.141.140:/mnt/ssd1/Paperless/media  /mnt/paperless-media  nfs4  rw,...  0  0
192.168.141.140:/mnt/ssd1                  /mnt/nas-legacy       nfs4  rw,...  0  0
```

| Mount auf CT 121 | NFS-Quelle | Inhalt |
|----------------|------------|--------|
| `/mnt/paperless-media` | `:/mnt/ssd1/Paperless/media` | Paperless-Archiv |
| `/mnt/nas-legacy` | `:/mnt/ssd1` | **ganzes ssd1** (nur dieser Branch — kein mergerfs, kein ssd2) |

**Auf pi-nas gibt es kein `/mnt/nas-legacy`** — das ist nur der Mountpoint-Name auf CT 121.

### CT 121 — sichtbar unter `/mnt/nas-legacy` (verifiziert)

Mount: `192.168.141.140:/mnt/ssd1` → `/mnt/nas-legacy`

**Tatsächlich sichtbar** (nur exportierte/unterstützte Subtrees):

```text
/mnt/nas-legacy/Eltern/Finanzen     ← Legacy-Import
/mnt/nas-legacy/Paperless/…
```

**Nicht sichtbar** auf CT 121 (trotz `/mnt/ssd1/Thomas` auf pi-nas):

```text
/mnt/nas-legacy/Thomas   → No such file or directory
/mnt/nas-legacy/Monika   → No such file or directory
```

Ursache: pi-nas `/etc/exports` exportiert typischerweise nur `…/Paperless/media` und `…/Eltern` — **nicht** `Thomas`/`Monika`. Der fstab-Eintrag `:/mnt/ssd1` zeigt auf dem Client nur die freigegebenen Zweige.

Thomas/Monika liegen physisch unter `/mnt/ssd1/Thomas`, `/mnt/ssd1/Monika` (und teils ssd2); für Import **zusätzliche Export-Zeilen** + Mount auf CT 121 nötig (siehe Abschnitt 1.1).

### 1.1 Thomas / Monika — NFS ergänzen (pi-nas)

```text
# /etc/exports — Beispiel (fsid neu, no_root_squash wegen 2770 thomas:thomas / monika:monika)
/mnt/ssd1/Thomas  192.168.131.31(ro,sync,no_subtree_check,no_root_squash,fsid=3)
/mnt/ssd1/Monika  192.168.131.31(ro,sync,no_subtree_check,no_root_squash,fsid=4)
```

Optional mergerfs-Gesamtsicht (ssd1+ssd2): stattdessen `/srv/nas/Thomas` exportieren — nur wenn gewollt.

```bash
exportfs -rav
```

**CT 121** — separate Mountpoints (fstab):

```fstab
192.168.141.140:/mnt/ssd1/Thomas  /mnt/nas-thomas  nfs4  ro,nfsvers=4.2,soft,timeo=600,retrans=2,_netdev  0  0
192.168.141.140:/mnt/ssd1/Monika  /mnt/nas-monika  nfs4  ro,nfsvers=4.2,soft,timeo=600,retrans=2,_netdev  0  0
```

```bash
mkdir -p /mnt/nas-thomas /mnt/nas-monika
mount -a
ls /mnt/nas-thomas /mnt/nas-monika | head
```

Import dann mit `LEGACY_NAS_FINANZEN=/mnt/nas-thomas` bzw. `/mnt/nas-monika` — **nicht** `/mnt/nas-legacy/Thomas`.

### mergerfs vs. ssd1-Mount (wichtig)

Auf **pi-nas** zeigt `/srv/nas/Thomas` und `/srv/nas/Monika` die **mergerfs**-Sicht (ssd1 **+** ssd2).

CT 121 sieht nur **`/mnt/ssd1/...`** — Dateien, die **nur auf ssd2** liegen, fehlen im Import.

Vor Thomas/Monika auf pi-nas prüfen:

```bash
# Gibt es Inhalte nur auf ssd2?
diff -rq /mnt/ssd1/Thomas /mnt/ssd2/Thomas 2>/dev/null | head
diff -rq /mnt/ssd1/Monika /mnt/ssd2/Monika 2>/dev/null | head
find /mnt/ssd2/Thomas /mnt/ssd2/Monika -type f 2>/dev/null | wc -l
```

Falls ssd2-only Dateien relevant sind: Export `/srv/nas` (ro) **oder** Konsolidierung auf ssd1 **vor** Import — nicht blind nur ssd1-Pfad scannen.

## 2. Legacy Finanzen — wie migriert (Checksum-Delta)

**Nicht** manuell `copy-missing` in einer Schleife. **Nicht** `done.lst`.

### State-Dateien (`/mnt/paperless-data/legacy-migrate/`)

| Datei | Rolle |
|-------|--------|
| `nas-sha256.tsv` | NAS-Inventar (SHA256 aller PDFs unter `NAS_ROOT`) |
| `paperless-checksums.tsv` | MD5 aller Docs aus Paperless metadata-API |
| `nas-missing-import.tsv` | **Queue:** eindeutige Inhalte, die in Paperless fehlen |
| `nas-in-flight.tsv` | Poppt pro Chunk — wartet auf Import-Bestätigung |

Live-Aktualisierung: nach jedem Chunk **reconcile** — nur Checksums, die Paperless bestätigt, fliegen raus; Fehlschläge zurück in `missing.tsv`.

### Ablauf (einmalig + Loop)

```bash
# NAS_ROOT = echter Finanzen-Pfad auf CT 121 (siehe Abschnitt 2)
export LEGACY_NAS_FINANZEN=/mnt/…/Finanzen   # Beispiel: /mnt/eltern-nas/Finanzen
export LEGACY_MIGRATE_STATE_DIR=/mnt/paperless-data/legacy-migrate

/opt/paperless-scripts/legacy-nas-sha256.sh scan
/opt/paperless-scripts/legacy-nas-sha256.sh fetch-paperless --refresh-paperless
/opt/paperless-scripts/legacy-nas-sha256.sh missing

tmux new -s legacy
/opt/paperless-scripts/legacy-nas-sha256.sh import-loop --batch queue --chunk 20
```

Pro Chunk: pop → `consume/legacy/queue/` → warten → reconcile.

**Fertig wenn:** `missing` meldet `0 einzigartige fehlende` (Stand: 2199 Docs in Paperless, Finanzen komplett).

### Veraltet

| Veraltet | Stattdessen |
|----------|-------------|
| `copy-missing` manuell wiederholen | `import-loop` |
| `nas-missing-done.lst` | `in-flight.tsv` + reconcile |
| `legacy-migrate-all.sh` / `legacy-migrate-resume.sh` | obiger Ablauf |

---

## 3. Thomas / Monika (Legacy-Modus — wie Finanzen / Gemeinsam)

Voraussetzung: NFS-Mount auf CT 121 existiert (Abschnitt 2).

**Wie Finanzen:** Pfad muss **`/legacy/`** enthalten → `pre_consume` überspringt OCR, `post_consume` **keine** Vision/LLM — nur Tag `legacy` + Speicherpfad `legacy/{title}`.

`.env` auf CT 121 (wie Finanzen-Migration):

```bash
LEGACY_CONSUME_MARKERS=/legacy/
LEGACY_TAG=legacy
LEGACY_STORAGE_PATH_TEMPLATE=legacy/{title}
```

Import-Ziel: **`/mnt/paperless-data/consume/legacy/thomas-inbox/`** (nicht `consume/thomas-inbox/` ohne `legacy`!).

```bash
export LEGACY_NAS_FINANZEN=/mnt/nas-thomas
export LEGACY_MIGRATE_STATE_DIR=/mnt/paperless-data/migrate-thomas
export LEGACY_PL_CHECKSUM_CACHE=/mnt/paperless-data/legacy-migrate/paperless-checksums.tsv
export LEGACY_CONSUME_ROOT=/mnt/paperless-data/consume/legacy

/opt/paperless-scripts/legacy-nas-sha256.sh scan
/opt/paperless-scripts/legacy-nas-sha256.sh vs-paperless
/opt/paperless-scripts/legacy-nas-sha256.sh missing

/opt/paperless-scripts/legacy-nas-sha256.sh import-loop --batch thomas-inbox --chunk 10
```

Monika PDF analog: `migrate-monika`, `consume/legacy/monika-inbox`, optional `EXCLUDE_REGEX`.

Grössen (pi-nas, mergerfs): Thomas ~81 eindeutige PDFs; Monika ~94 eindeutige PDFs (+ Fotos separat entscheiden).

---

## 4. Checkliste vor jedem neuen Import

0. **consume leer?** (siehe unten)
1. pi-nas: physischer Pfad + Zählung (`/mnt/ssd1/...`, ggf. ssd2-Diff)
2. pi-nas: Export + `exportfs -rav` für neue Quellen (Thomas/Monika)
3. CT 121: `findmnt`, neuer Mount sichtbar (`/mnt/nas-thomas` …)
4. CT 121: `LEGACY_NAS_FINANZEN` auf **diesen** Mount
5. Eigenes `LEGACY_MIGRATE_STATE_DIR` pro Quelle
6. `scan` → `missing` → `import-loop`

### consume prüfen (CT 121)

```bash
/opt/paperless-scripts/legacy-tasks-summary.sh

echo "=== consume gesamt ==="
find /mnt/paperless-data/consume -type f \( -iname '*.pdf' -o -iname '*.jpg' -o -iname '*.png' \) 2>/dev/null | wc -l

for d in /mnt/paperless-data/consume/*/ /mnt/paperless-data/consume/legacy/*/; do
  [[ -d "$d" ]] || continue
  n=$(find "$d" -type f 2>/dev/null | wc -l)
  echo "  $(basename "$d"): $n Dateien"
done
```

Vor neuem Import: consume leer oder bewusst nur ein Batch; `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` in `.env`.
