# NAS → Paperless: NFS und Import-Pfade

CT 121 mountet das NAS per NFS. Konkrete Export-Pfade, UIDs und Squash-Optionen stehen in der privaten Betriebsdoku: [ct121-nfs-fix.md](../../../doku/pve2/vm/121-paperless/Doku/docs/ct121-nfs-fix.md).

Dieses Dokument beschreibt das **Muster** (zwei Mounts, Import-Pfade) — nicht den älteren Soll-Entwurf mit `:/srv/nas` → `/mnt/nas-legacy`, falls der nie so eingespielt wurde.

Siehe auch: [LEGACY_MIGRATION_PLAN.md](./LEGACY_MIGRATION_PLAN.md)

Platzhalter: `<NAS-IP>` = NAS, `<CT-IP>` = CT 121. Pfade und `fsid` aus der Betriebsdoku übernehmen.

---

## 1. NFS-Muster

### pi-nas — `/etc/exports` (Form)

```text
<media-export>   <CT-IP>(rw,...,all_squash,anonuid=<uid>,anongid=<gid>,fsid=<n>)
<legacy-export>  <CT-IP>(ro,...,fsid=<n>)
```

**Hinweis:** Wenn CT 121 das übergeordnete Volume mountet, muss dieses Volume (oder ein gleichwertiger Export) für `<CT-IP>` freigegeben sein — sonst widersprechen exports und fstab. Nach Änderung: `exportfs -rav`.

### CT 121 — `/etc/fstab` (Form)

```fstab
<NAS-IP>:<media-export>   /mnt/paperless-media  nfs4  rw,...  0  0
<NAS-IP>:<legacy-export>  /mnt/nas-legacy       nfs4  rw,...  0  0
```

| Mount auf CT 121 | NFS-Quelle | Inhalt |
|----------------|------------|--------|
| `/mnt/paperless-media` | Media-Export | Paperless-Archiv |
| `/mnt/nas-legacy` | Legacy-Export | nur die freigegebenen Zweige, nicht zwingend das ganze Volume |

**Auf dem NAS gibt es kein `/mnt/nas-legacy`** — das ist nur der Mountpoint-Name auf CT 121.

Ein Mount des gesamten Volumes zeigt auf dem Client nur die in `exports` freigegebenen Unterbäume. Zusätzliche Quellen (`user-a`, `user-b`) brauchen eigene Export-Zeilen und eigene Mountpoints — ein Unterordner, den der Volume-Mount nicht zeigt, ist auf CT 121 nicht lesbar.

### 1.1 user-a / user-b — NFS ergänzen

```text
# /etc/exports — Form (fsid neu; Squash-Option aus der Betriebsdoku)
<user-a-export>  <CT-IP>(ro,sync,no_subtree_check,fsid=<n>)
<user-b-export>  <CT-IP>(ro,sync,no_subtree_check,fsid=<n>)
```

Optional die zusammengeführte Sicht (mehrere Platten) exportieren — nur wenn gewollt.

```bash
exportfs -rav
```

**CT 121** — eigene Mountpoints (fstab):

```fstab
<NAS-IP>:<user-a-export>  /mnt/nas-user-a  nfs4  ro,nfsvers=4.2,soft,timeo=600,retrans=2,_netdev  0  0
<NAS-IP>:<user-b-export>  /mnt/nas-user-b  nfs4  ro,nfsvers=4.2,soft,timeo=600,retrans=2,_netdev  0  0
```

```bash
mkdir -p /mnt/nas-user-a /mnt/nas-user-b
mount -a
ls /mnt/nas-user-a /mnt/nas-user-b | head
```

Import dann mit `LEGACY_NAS_FINANZEN=/mnt/nas-user-a` bzw. `/mnt/nas-user-b` — **nicht** über einen Unterordner, den der Volume-Mount nicht zeigt.

### mergerfs vs. einzelnes Volume

Wenn das NAS eine mergerfs-Sicht über mehrere Platten hat, sieht CT 121 nur das exportierte Volume. Dateien, die nur auf einer anderen Platte liegen, fehlen im Import.

Vor weiteren Quellen auf dem NAS prüfen, ob Inhalte ausserhalb des exportierten Volumes liegen. Falls ja: die zusammengeführte Sicht exportieren oder vorher auf das exportierte Volume konsolidieren — nicht blind nur einen Plattenpfad scannen.

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
export LEGACY_NAS_FINANZEN=/mnt/<familien-mount>/Finanzen
export LEGACY_MIGRATE_STATE_DIR=/mnt/paperless-data/legacy-migrate

/opt/paperless-scripts/legacy-nas-sha256.sh scan
/opt/paperless-scripts/legacy-nas-sha256.sh fetch-paperless --refresh-paperless
/opt/paperless-scripts/legacy-nas-sha256.sh missing

tmux new -s legacy
/opt/paperless-scripts/legacy-nas-sha256.sh import-loop --batch queue --chunk 20
```

Pro Chunk: pop → `consume/legacy/queue/` → warten → reconcile.

**Fertig wenn:** `missing` meldet `0 einzigartige fehlende`.

### Veraltet

| Veraltet | Stattdessen |
|----------|-------------|
| `copy-missing` manuell wiederholen | `import-loop` |
| `nas-missing-done.lst` | `in-flight.tsv` + reconcile |
| `legacy-migrate-all.sh` / `legacy-migrate-resume.sh` | obiger Ablauf |

---

## 3. user-a / user-b (Legacy-Modus — wie Finanzen / Gemeinsam)

Voraussetzung: NFS-Mount auf CT 121 existiert (Abschnitt 2).

**Wie Finanzen:** Pfad muss **`/legacy/`** enthalten → `pre_consume` überspringt OCR, `post_consume` **keine** Vision/LLM — nur Tag `legacy` + Speicherpfad `legacy/{title}`.

`.env` auf CT 121 (wie Finanzen-Migration):

```bash
LEGACY_CONSUME_MARKERS=/legacy/
LEGACY_TAG=legacy
LEGACY_STORAGE_PATH_TEMPLATE=legacy/{title}
```

Import-Ziel: **`/mnt/paperless-data/consume/legacy/user-a-inbox/`** (nicht `consume/user-a-inbox/` ohne `legacy`!).

```bash
export LEGACY_NAS_FINANZEN=/mnt/nas-user-a
export LEGACY_MIGRATE_STATE_DIR=/mnt/paperless-data/migrate-user-a
export LEGACY_PL_CHECKSUM_CACHE=/mnt/paperless-data/legacy-migrate/paperless-checksums.tsv
export LEGACY_CONSUME_ROOT=/mnt/paperless-data/consume/legacy

/opt/paperless-scripts/legacy-nas-sha256.sh scan
/opt/paperless-scripts/legacy-nas-sha256.sh vs-paperless
/opt/paperless-scripts/legacy-nas-sha256.sh missing

/opt/paperless-scripts/legacy-nas-sha256.sh import-loop --batch user-a-inbox --chunk 10
```

user-b PDF analog: `migrate-user-b`, `consume/legacy/user-b-inbox`, optional `EXCLUDE_REGEX`.

---

## 4. Checkliste vor jedem neuen Import

0. **consume leer?** (siehe unten)
1. pi-nas: physischer Pfad + Zählung (exportiertes Volume, ggf. Diff gegen weitere Platten)
2. pi-nas: Export + `exportfs -rav` für neue Quellen (user-a/user-b)
3. CT 121: `findmnt`, neuer Mount sichtbar (`/mnt/nas-user-a` …)
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
