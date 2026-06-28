#!/usr/bin/env bash
# NAS-Altbestand: SHA256-Inventar, interne Dubletten, optional Abgleich mit Paperless-Checksums.
#
# Auf CT 121 (nach NFS-Mount):
#   ./scripts/legacy-nas-sha256.sh scan          # ~2300 PDFs, dauert einige Minuten
#   ./scripts/legacy-nas-sha256.sh summary       # Kurzstatistik
#   ./scripts/legacy-nas-sha256.sh duplicates    # Dubletten-Gruppen (TSV)
#   ./scripts/legacy-nas-sha256.sh vs-paperless  # erwartete Import-Dubletten
#   ./scripts/legacy-nas-sha256.sh missing      # Delta-Liste neu (langsam, einmalig)
#   ./scripts/legacy-nas-sha256.sh import-chunk --batch queue --chunk 20
#   ./scripts/legacy-nas-sha256.sh import-loop --batch queue --chunk 20   # empfohlen (tmux)
#
set -euo pipefail

NAS_ROOT="${LEGACY_NAS_FINANZEN:-/mnt/nas-legacy/Eltern/Finanzen}"
STATE_DIR="${LEGACY_MIGRATE_STATE_DIR:-/mnt/paperless-data/legacy-migrate}"
CONSUME_ROOT="${LEGACY_CONSUME_ROOT:-/mnt/paperless-data/consume/legacy}"
ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"
INVENTORY="${LEGACY_NAS_SHA256_TSV:-$STATE_DIR/nas-sha256.tsv}"
DUPES_TSV="${LEGACY_NAS_DUPES_TSV:-$STATE_DIR/nas-duplicates.tsv}"
MISSING_TSV="${LEGACY_NAS_MISSING_TSV:-$STATE_DIR/nas-missing-import.tsv}"
IN_FLIGHT_TSV="${LEGACY_NAS_IN_FLIGHT:-$STATE_DIR/nas-in-flight.tsv}"
SUMMARY_FILE="${LEGACY_NAS_SHA256_SUMMARY:-$STATE_DIR/nas-sha256-summary.txt}"
STALL_SLEEP="${LEGACY_STALL_SLEEP:-30}"
EXCLUDE_REGEX="${EXCLUDE_REGEX:-}"
# Standard: Moni 2015/2016 aus Migration-Plan ausschliessen (| als Trenner), z.B.:
# EXCLUDE_REGEX='Vorsorge/Moni/2015|Vorsorge/Moni/2016'
PL_CACHE="${LEGACY_PL_CHECKSUM_CACHE:-$STATE_DIR/paperless-checksums.tsv}"

CMD="${1:-summary}"
shift || true

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  echo ""
  echo "Befehle: scan | summary | duplicates | vs-paperless | fetch-paperless | missing"
  echo "         prune-missing | reconcile | copy-missing | import-chunk | import-loop | all"
  echo ""
  echo "Import (empfohlen):  import-loop --batch queue --chunk 20"
  echo "  Pro Chunk: pop aus Delta → consume → warten → reconcile (Paperless-Wahrheit)"
  echo ""
  echo "State: missing=$MISSING_TSV | in-flight=$IN_FLIGHT_TSV"
}

path_excluded() {
  local rel="$1"
  [[ -z "$EXCLUDE_REGEX" ]] && return 1
  [[ "$rel" =~ ($EXCLUDE_REGEX) ]]
}

top_folder() {
  local rel="$1"
  local rest="${rel#*/}"
  if [[ "$rest" == "$rel" || -z "$rest" ]]; then
    echo "(root)"
  else
    echo "${rel%%/*}"
  fi
}

scan_nas() {
  local refresh=0 n=0 skipped=0 cached=0 hashed=0 start
  start=$(date +%s)

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --refresh) refresh=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
  done

  [[ -d "$NAS_ROOT" ]] || { echo "FEHLER: NAS nicht gemountet: $NAS_ROOT" >&2; exit 1; }
  mkdir -p "$STATE_DIR"

  local tmp old
  tmp=$(mktemp)
  old=$(mktemp)
  [[ -f "$INVENTORY" ]] && cp "$INVENTORY" "$old"

  printf '%s\n' 'relpath	size_bytes	mtime_epoch	sha256' >"$tmp"

  while IFS= read -r -d '' f; do
    local rel size mtime sha line reuse=0
    rel="${f#$NAS_ROOT/}"
    rel="${rel#/}"

    if path_excluded "$rel"; then
      skipped=$((skipped + 1))
      continue
    fi

    size=$(stat -c '%s' "$f")
    mtime=$(stat -c '%Y' "$f")
    sha=""

    if [[ "$refresh" -eq 0 && -f "$old" ]]; then
      line=$(awk -F'\t' -v p="$rel" -v s="$size" -v m="$mtime" '
        NR > 1 && $1 == p && $2 == s && $3 == m { print $4; exit }
      ' "$old" || true)
      if [[ -n "$line" ]]; then
        sha="$line"
        cached=$((cached + 1))
        reuse=1
      fi
    fi

    if [[ "$reuse" -eq 0 ]]; then
      sha=$(sha256sum "$f" | awk '{print $1}')
      hashed=$((hashed + 1))
    fi

    printf '%s\t%s\t%s\t%s\n' "$rel" "$size" "$mtime" "$sha" >>"$tmp"
    n=$((n + 1))
    if (( n % 50 == 0 )); then
      echo "[scan] $n PDFs … (neu gehasht: $hashed, Cache: $cached)" >&2
    fi
  done < <(find "$NAS_ROOT" -type f \( -iname '*.pdf' \) -print0 | sort -z)

  mv "$tmp" "$INVENTORY"
  rm -f "$old"

  local elapsed=$(( $(date +%s) - start ))
  echo "Scan fertig: $n PDFs in $INVENTORY (${elapsed}s, neu gehasht: $hashed, Cache: $cached, ausgeschlossen: $skipped)"
}

run_python() {
  export PL_CACHE REFRESH_PL
  python3 - "$@" <<'PY'
import json
import os
import sys
import urllib.request
from collections import defaultdict

cmd = sys.argv[1]
inventory = os.environ.get("INVENTORY", "")
dupes_tsv = os.environ.get("DUPES_TSV", "")
summary_file = os.environ.get("SUMMARY_FILE", "")
nas_root = os.environ.get("NAS_ROOT", "")
env_file = os.environ.get("ENV_FILE", "/opt/paperless/.env")
pl_cache = os.environ.get("PL_CACHE", "")
missing_tsv = os.environ.get("MISSING_TSV", "")
in_flight_tsv = os.environ.get("IN_FLIGHT_TSV", "")
consume_dest = os.environ.get("CONSUME_DEST", "")
pop_chunk = int(os.environ.get("POP_CHUNK", "0") or "0")
refresh_pl = os.environ.get("REFRESH_PL", "0") == "1"
min_group = int(os.environ.get("MIN_GROUP", "2"))


def api_get(token, url):
    req = urllib.request.Request(url, headers={"Authorization": f"Token {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def read_token():
    if not os.path.isfile(env_file):
        return ""
    for line in open(env_file, encoding="utf-8", errors="replace"):
        if line.startswith("PAPERLESS_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def fetch_doc_ids(token):
    ids = []
    api_total = 0
    url = "http://127.0.0.1:8000/api/documents/?page_size=100"
    while url:
        data = api_get(token, url)
        if isinstance(data, list):
            results = data
            url = None
        else:
            results = data.get("results", [])
            if api_total == 0:
                api_total = int(data.get("count", 0) or 0)
            url = data.get("next")
        for doc in results:
            doc_id = doc.get("id")
            if doc_id is not None:
                ids.append(int(doc_id))
    if api_total == 0:
        api_total = len(ids)
    return ids, api_total


def save_pl_cache(checksums_by_id):
    if not pl_cache:
        return
    with open(pl_cache, "w", encoding="utf-8") as f:
        f.write("doc_id\tchecksum\n")
        for doc_id in sorted(checksums_by_id):
            f.write(f"{doc_id}\t{checksums_by_id[doc_id]}\n")


def fetch_paperless_checksums():
    token = read_token()
    if not token:
        print("FEHLER: kein PAPERLESS_TOKEN in", env_file, file=sys.stderr)
        sys.exit(1)

    doc_ids, api_total = fetch_doc_ids(token)
    if not doc_ids:
        return {}, 0

    # Checksum steht nicht in /api/documents/ — nur in /metadata/ oder DB (kein Custom Field!)
    checksums_by_id = {}
    source = "metadata"

    if not refresh_pl and pl_cache and os.path.isfile(pl_cache):
        cached = {}
        with open(pl_cache, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("doc_id") or not line.strip():
                    continue
                parts = line.strip().split("\t")
                if len(parts) >= 2 and parts[1]:
                    cached[int(parts[0])] = parts[1].lower()
        if len(cached) == len(doc_ids):
            checksums_by_id = cached
            source = "cache"

    if not checksums_by_id:
        print(
            f"Lese original_checksum via /api/documents/{{id}}/metadata/ ({len(doc_ids)} Docs) …",
            file=sys.stderr,
        )
        for i, doc_id in enumerate(doc_ids):
            if i > 0 and i % 100 == 0:
                print(f"  … {i}/{len(doc_ids)}", file=sys.stderr)
            try:
                meta = api_get(token, f"http://127.0.0.1:8000/api/documents/{doc_id}/metadata/")
            except Exception as e:
                print(f"WARN: metadata #{doc_id}: {e}", file=sys.stderr)
                continue
            cs = (meta.get("original_checksum") or meta.get("checksum") or "").strip().lower()
            if cs:
                checksums_by_id[doc_id] = cs
        save_pl_cache(checksums_by_id)

    checksums = {cs: doc_id for doc_id, cs in checksums_by_id.items()}
    print(f"Paperless-Checksums: {len(checksums)} ({source})", file=sys.stderr)
    return checksums, api_total


def load_inventory(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        f.readline()  # header
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            rel, size, mtime, sha = parts[0], parts[1], parts[2], parts[3]
            rows.append((rel, int(size), int(mtime), sha.lower()))
    return rows


def md5_file(path):
    import hashlib

    h = hashlib.md5()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def top_folder(rel):
    if "/" not in rel:
        return "(root)"
    return rel.split("/", 1)[0]


def analyze(rows):
    by_hash = defaultdict(list)
    by_folder = defaultdict(lambda: {"files": 0, "unique": set()})
    for rel, size, _mtime, sha in rows:
        by_hash[sha].append((rel, size))
        tf = top_folder(rel)
        by_folder[tf]["files"] += 1
        by_folder[tf]["unique"].add(sha)

    total = len(rows)
    unique = len(by_hash)
    dup_files = total - unique
    dup_groups = sum(1 for paths in by_hash.values() if len(paths) > 1)
    extra_copies = sum(len(paths) - 1 for paths in by_hash.values() if len(paths) > 1)
    return by_hash, by_folder, total, unique, dup_files, dup_groups, extra_copies


def print_summary(rows):
    by_hash, by_folder, total, unique, dup_files, dup_groups, extra = analyze(rows)

    lines = [
        "=== NAS SHA256 Inventar ===",
        f"Quelle:     {nas_root}",
        f"Inventar:   {inventory}",
        f"PDF-Dateien gesamt:        {total}",
        f"Einzigartige Inhalte (SHA): {unique}",
        f"Dubletten-Dateien (Kopien): {dup_files}  (= gesamt − eindeutig)",
        f"Dubletten-Gruppen:         {dup_groups}  (Hashes mit ≥2 Dateien)",
        f"Überzählige Kopien:        {extra}  (importierbar als 1 Doc pro Hash)",
        "",
        "Pro Ordner (Dateien / eindeutige Hashes / erwartete NAS-Kopien):",
    ]
    for folder in sorted(by_folder):
        info = by_folder[folder]
        files = info["files"]
        u = len(info["unique"])
        copies = files - u
        lines.append(f"  {folder}: {files} Dateien, {u} eindeutig, {copies} NAS-Kopien")

    lines.append("")
    lines.append("Erwartung beim Import (nur NAS-intern):")
    lines.append(f"  Max. neue Docs wenn Paperless leer wäre: {unique}")
    lines.append(f"  Sinnlose Re-Imports derselben Bytes:    {dup_files}")

    text = "\n".join(lines)
    print(text)
    if summary_file:
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(text + "\n")


def write_duplicates(rows):
    by_hash, _, total, unique, dup_files, dup_groups, _extra = analyze(rows)
    groups = [(sha, paths) for sha, paths in by_hash.items() if len(paths) >= min_group]
    groups.sort(key=lambda x: (-len(x[1]), x[0]))

    with open(dupes_tsv, "w", encoding="utf-8") as out:
        out.write("sha256\tcopy_count\trelpath\tsize_bytes\n")
        for sha, paths in groups:
            paths_sorted = sorted(paths, key=lambda x: x[0])
            for i, (rel, size) in enumerate(paths_sorted):
                out.write(f"{sha}\t{len(paths)}\t{rel}\t{size}\n")

    print(f"Dubletten-Gruppen (>= {min_group}): {len(groups)}")
    print(f"Dubletten-Dateien gesamt: {dup_files} ({total} Dateien, {unique} eindeutig)")
    print(f"Details: {dupes_tsv}")
    print("")
    print("Top-10 Gruppen (meiste Kopien):")
    for sha, paths in groups[:10]:
        print(f"  {len(paths)}× {sha[:16]}…  z.B. {paths[0][0]}")


def detect_checksum_algo(checksums):
    lengths = {len(c) for c in checksums}
    if 64 in lengths:
        return "sha256"
    if 32 in lengths:
        return "md5"
    return "unknown"


def cmd_fetch_paperless():
    checksums, api_total = fetch_paperless_checksums()
    algo = detect_checksum_algo(checksums)
    print(f"Gecacht: {pl_cache}")
    print(f"Dokumente: {api_total}, Checksums: {len(checksums)}, Algorithmus: {algo}")


def nas_compare_hash(rel, sha256, algo):
    path = os.path.join(nas_root, rel)
    if algo == "sha256":
        return sha256
    if algo == "md5":
        return md5_file(path)
    return sha256


def build_nas_by_compare(rows, algo):
    nas_by_compare = defaultdict(list)
    if algo == "md5":
        print("Berechne MD5 für NAS-Dateien (Paperless-Format) …", file=sys.stderr)
    for i, (rel, _size, _mtime, sha) in enumerate(rows):
        if algo == "md5" and i > 0 and i % 200 == 0:
            print(f"  … {i}/{len(rows)}", file=sys.stderr)
        ch = nas_compare_hash(rel, sha, algo)
        nas_by_compare[ch].append(rel)
    return nas_by_compare


def cmd_missing(rows):
    if not pl_cache or not os.path.isfile(pl_cache):
        print("FEHLER: kein Paperless-Cache — zuerst: fetch-paperless", file=sys.stderr)
        sys.exit(1)

    pl_checksums, api_total = fetch_paperless_checksums()
    algo = detect_checksum_algo(pl_checksums)
    if not pl_checksums:
        print("FEHLER: keine Paperless-Checksums", file=sys.stderr)
        sys.exit(1)

    nas_by_compare = build_nas_by_compare(rows, algo)
    pl_hashes = set(pl_checksums)
    new_hashes = sorted(set(nas_by_compare) - pl_hashes)

    entries = []
    skipped_copies = 0
    for ch in new_hashes:
        paths = sorted(nas_by_compare[ch])
        canonical = paths[0]
        skipped_copies += len(paths) - 1
        sha = ""
        for rel, _s, _m, sha256 in rows:
            if rel == canonical:
                sha = sha256
                break
        entries.append((canonical, ch, len(paths), sha))

    if not missing_tsv:
        print("FEHLER: MISSING_TSV nicht gesetzt", file=sys.stderr)
        sys.exit(1)

    with open(missing_tsv, "w", encoding="utf-8") as f:
        f.write("relpath\tchecksum\tnas_copies\tsha256\n")
        for rel, ch, copies, sha in entries:
            f.write(f"{rel}\t{ch}\t{copies}\t{sha}\n")

    print("=== NAS fehlt in Paperless (Import-Queue) ===")
    print(f"Paperless Docs:           {api_total}")
    print(f"Einzigartige fehlende:    {len(entries)}  (= neue Docs wenn importiert)")
    print(f"NAS-Kopien übersprungen:  {skipped_copies}  (pro Hash nur 1 Pfad)")
    print(f"Liste:                    {missing_tsv}")
    print("")
    print("Top-Ordner:")
    by_folder = defaultdict(int)
    for rel, _ch, _c, _sha in entries:
        by_folder[top_folder(rel)] += 1
    for folder, n in sorted(by_folder.items(), key=lambda x: -x[1])[:12]:
        print(f"  {folder}: {n}")


def read_data_lines(path):
    if not path or not os.path.isfile(path):
        return []
    lines = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("relpath"):
                continue
            lines.append(line)
    return lines


def write_data_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("relpath\tchecksum\tnas_copies\tsha256\n")
        for line in lines:
            f.write(line + "\n")


def append_data_lines(path, lines):
    if not lines:
        return
    new_file = not os.path.isfile(path)
    with open(path, "a", encoding="utf-8") as f:
        if new_file:
            f.write("relpath\tchecksum\tnas_copies\tsha256\n")
        for line in lines:
            f.write(line + "\n")


def cmd_pop_missing():
    """N Zeilen von missing → in-flight (nicht erst beim Import als done markieren)."""
    if not missing_tsv or not os.path.isfile(missing_tsv):
        print("FEHLER: keine missing.tsv", file=sys.stderr)
        sys.exit(1)
    if pop_chunk <= 0:
        print("FEHLER: POP_CHUNK fehlt", file=sys.stderr)
        sys.exit(1)

    pending = read_data_lines(missing_tsv)
    popped = []
    kept = []
    for line in pending:
        if len(popped) >= pop_chunk:
            kept.append(line)
            continue
        rel = line.split("\t", 1)[0]
        if consume_dest and os.path.isfile(os.path.join(consume_dest, rel)):
            kept.append(line)
            continue
        popped.append(line)

    write_data_lines(missing_tsv, kept)
    append_data_lines(in_flight_tsv, popped)
    for line in popped:
        print(line.split("\t", 1)[0])


def cmd_reconcile():
    """in-flight gegen Paperless prüfen; missing von importierten Checksums säubern."""
    pl_checksums, api_total = fetch_paperless_checksums()
    if not pl_checksums:
        print("FEHLER: keine Paperless-Checksums", file=sys.stderr)
        sys.exit(1)
    pl_hashes = set(pl_checksums)

    imported = 0
    retry = []
    for line in read_data_lines(in_flight_tsv):
        parts = line.split("\t")
        ch = parts[1].strip().lower() if len(parts) >= 2 else ""
        if ch and ch in pl_hashes:
            imported += 1
        else:
            retry.append(line)

    missing_lines = read_data_lines(missing_tsv)
    pruned_missing = []
    pruned_dup = 0
    for line in missing_lines:
        parts = line.split("\t")
        ch = parts[1].strip().lower() if len(parts) >= 2 else ""
        if ch and ch in pl_hashes:
            pruned_dup += 1
        else:
            pruned_missing.append(line)

    # Fehlgeschlagene Imports zurück an den Anfang der Queue
    write_data_lines(missing_tsv, retry + pruned_missing)
    write_data_lines(in_flight_tsv, [])

    remaining = len(retry) + len(pruned_missing)
    print("=== Delta reconciled ===")
    print(f"Paperless Docs:           {api_total}")
    print(f"in-flight importiert:     {imported}")
    print(f"in-flight zurück (retry): {len(retry)}")
    print(f"missing bereinigt:        {pruned_dup}")
    print(f"Verbleibend gesamt:       {remaining}")


def cmd_prune_missing():
    """Nur missing.tsv bereinigen (ohne in-flight) — für manuelle Nutzung."""
    if not missing_tsv or not os.path.isfile(missing_tsv):
        print("FEHLER: keine missing.tsv — zuerst: missing", file=sys.stderr)
        sys.exit(1)

    pl_checksums, api_total = fetch_paperless_checksums()
    if not pl_checksums:
        print("FEHLER: keine Paperless-Checksums", file=sys.stderr)
        sys.exit(1)

    pl_hashes = set(pl_checksums)
    kept = []
    removed = 0
    for line in read_data_lines(missing_tsv):
        parts = line.split("\t")
        ch = parts[1].strip().lower() if len(parts) >= 2 else ""
        if ch in pl_hashes:
            removed += 1
            continue
        kept.append(line)

    write_data_lines(missing_tsv, kept)

    print("=== Delta aktualisiert (prune-missing) ===")
    print(f"Paperless Docs:        {api_total}")
    print(f"Entfernt (importiert): {removed}")
    print(f"Verbleibend:           {len(kept)}")
    print(f"Liste:                 {missing_tsv}")


def vs_paperless(rows):
    by_hash, _, total, unique, dup_files, dup_groups, _extra = analyze(rows)
    pl_checksums, api_total = fetch_paperless_checksums()
    algo = detect_checksum_algo(pl_checksums)

    if api_total == 0:
        print("WARN: Paperless API lieferte 0 Dokumente — Token/URL prüfen", file=sys.stderr)
    elif not pl_checksums:
        print(
            f"WARN: {api_total} Docs, aber keine Checksums aus metadata — Pfade/Token prüfen",
            file=sys.stderr,
        )

    nas_by_compare = build_nas_by_compare(rows, algo)
    pl_hashes = set(pl_checksums)
    overlap_hashes = set(nas_by_compare) & pl_hashes
    new_hashes = set(nas_by_compare) - pl_hashes

    files_already = sum(len(nas_by_compare[h]) for h in overlap_hashes)
    files_new_content = sum(len(nas_by_compare[h]) for h in new_hashes)
    realistic_new = max(0, unique - len(overlap_hashes))

    lines = [
        "",
        "=== NAS vs. Paperless (Checksum-Abgleich) ===",
        f"Dokumente in Paperless (API count): {api_total}",
        f"Docs mit original_checksum:        {len(pl_checksums)}",
        f"Paperless-Algorithmus:              {algo}",
        f"Einzigartige NAS-Inhalte (SHA256):  {unique}",
        f"Bereits in Paperless (Hash-Match):  {len(overlap_hashes)} Hashes → {files_already} NAS-Dateien",
        f"Nicht in Paperless:                 {len(new_hashes)} Hashes → {files_new_content} NAS-Dateien",
        "",
        "Erwartung beim Legacy-Import:",
        f"  ~{files_already} NAS-Dateien → Duplikat-Fehler (Inhalt schon in Paperless)",
        f"  ~{files_new_content} NAS-Dateien → versucht zu importieren",
        f"  Davon NAS-interne Kopien:         {dup_files} Dateien ({dup_groups} Gruppen)",
        f"  Max. neue Docs realistisch:       ~{realistic_new}",
        "",
        f"  Formel: eindeutige NAS ({unique}) − in Paperless ({len(overlap_hashes)}) ≈ {realistic_new}",
        "",
        "Hinweis: Checksum ist intern (metadata-API), kein Custom Field nötig.",
    ]
    text = "\n".join(lines)
    print(text)
    if summary_file and os.path.isfile(summary_file):
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if cmd == "summary":
    rows = load_inventory(inventory)
    if not rows:
        print(f"FEHLER: leeres Inventar — zuerst: scan", file=sys.stderr)
        sys.exit(1)
    print_summary(rows)
elif cmd == "duplicates":
    rows = load_inventory(inventory)
    if not rows:
        print(f"FEHLER: leeres Inventar — zuerst: scan", file=sys.stderr)
        sys.exit(1)
    write_duplicates(rows)
elif cmd == "vs-paperless":
    rows = load_inventory(inventory)
    if not rows:
        print(f"FEHLER: leeres Inventar — zuerst: scan", file=sys.stderr)
        sys.exit(1)
    vs_paperless(rows)
elif cmd == "fetch-paperless":
    cmd_fetch_paperless()
elif cmd == "missing":
    rows = load_inventory(inventory)
    if not rows:
        print(f"FEHLER: leeres Inventar — zuerst: scan", file=sys.stderr)
        sys.exit(1)
    cmd_missing(rows)
elif cmd == "prune-missing":
    cmd_prune_missing()
elif cmd == "pop-missing":
    cmd_pop_missing()
elif cmd == "reconcile":
    cmd_reconcile()
else:
    print(f"unbekannter python cmd: {cmd}", file=sys.stderr)
    sys.exit(1)
PY
}

cmd_summary() {
  [[ -f "$INVENTORY" ]] || { echo "FEHLER: kein Inventar $INVENTORY — zuerst: $0 scan" >&2; exit 1; }
  INVENTORY="$INVENTORY" DUPES_TSV="$DUPES_TSV" SUMMARY_FILE="$SUMMARY_FILE" NAS_ROOT="$NAS_ROOT" \
    run_python summary
}

cmd_duplicates() {
  local min=2
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --min) min="${2:?}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
  done
  [[ -f "$INVENTORY" ]] || { echo "FEHLER: kein Inventar — zuerst: $0 scan" >&2; exit 1; }
  INVENTORY="$INVENTORY" DUPES_TSV="$DUPES_TSV" MIN_GROUP="$min" NAS_ROOT="$NAS_ROOT" \
    run_python duplicates
}

cmd_vs_paperless() {
  local refresh=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --refresh-paperless) refresh=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
  done
  [[ -f "$INVENTORY" ]] || { echo "FEHLER: kein Inventar — zuerst: $0 scan" >&2; exit 1; }
  INVENTORY="$INVENTORY" SUMMARY_FILE="$SUMMARY_FILE" NAS_ROOT="$NAS_ROOT" ENV_FILE="$ENV_FILE" \
    PL_CACHE="$PL_CACHE" REFRESH_PL="$refresh" \
    run_python vs-paperless
}

cmd_fetch_paperless() {
  local refresh=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --refresh-paperless) refresh=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
  done
  PL_CACHE="$PL_CACHE" REFRESH_PL="$refresh" ENV_FILE="$ENV_FILE" NAS_ROOT="$NAS_ROOT" \
    run_python fetch-paperless
}

cmd_reconcile() {
  [[ -f "$MISSING_TSV" ]] || { echo "FEHLER: $MISSING_TSV fehlt" >&2; exit 1; }
  MISSING_TSV="$MISSING_TSV" IN_FLIGHT_TSV="$IN_FLIGHT_TSV" ENV_FILE="$ENV_FILE" \
    PL_CACHE="$PL_CACHE" REFRESH_PL=1 \
    run_python reconcile
}

cmd_prune_missing() {
  [[ -f "$MISSING_TSV" ]] || { echo "FEHLER: $MISSING_TSV fehlt" >&2; exit 1; }
  MISSING_TSV="$MISSING_TSV" ENV_FILE="$ENV_FILE" PL_CACHE="$PL_CACHE" REFRESH_PL=1 \
    run_python prune-missing
}

cmd_pop_missing() {
  local chunk="$1" dest="$2"
  MISSING_TSV="$MISSING_TSV" IN_FLIGHT_TSV="$IN_FLIGHT_TSV" CONSUME_DEST="$dest" POP_CHUNK="$chunk" \
    run_python pop-missing
}

missing_count() {
  local n=0 m=0
  [[ -f "$MISSING_TSV" ]] && n=$(read_data_lines_count "$MISSING_TSV")
  [[ -f "$IN_FLIGHT_TSV" ]] && m=$(read_data_lines_count "$IN_FLIGHT_TSV")
  echo $((n + m))
}

read_data_lines_count() {
  local f="$1"
  [[ -f "$f" ]] || { echo 0; return; }
  awk 'NR>1 && NF {c++} END{print c+0}' "$f"
}

pending_count() {
  read_data_lines_count "$MISSING_TSV"
}

in_flight_count() {
  read_data_lines_count "$IN_FLIGHT_TSV"
}

wait_consume_empty() {
  local dest="$1"
  local idle=0 last=-1 n=0
  echo "Warte: consume leer in $dest …"
  while true; do
    n=$(find "$dest" -type f -iname '*.pdf' 2>/dev/null | wc -l | tr -d ' ')
    [[ "$n" -eq 0 ]] && break
    if [[ "$n" -eq "$last" ]]; then
      idle=$((idle + 1))
      [[ "$idle" -ge 20 ]] && {
        echo "WARN: $n PDFs seit $((idle * STALL_SLEEP))s in consume — Paperless prüfen" >&2
        break
      }
    else
      idle=0
      last=$n
    fi
    echo "  noch $n PDFs …"
    sleep "$STALL_SLEEP"
  done
  echo "consume leer."
}

cmd_copy_missing() {
  local batch="queue" chunk=0 dry=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --batch) batch="${2:?}"; shift 2 ;;
      --chunk) chunk="${2:?}"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
  done

  [[ -f "$MISSING_TSV" ]] || {
    echo "FEHLER: $MISSING_TSV fehlt — zuerst: $0 missing" >&2
    exit 1
  }
  [[ "$chunk" -gt 0 ]] || {
    echo "FEHLER: --chunk N erforderlich" >&2
    exit 1
  }

  local dest="$CONSUME_ROOT/$batch"
  local copied=0 rel src dest_file dest_dir
  local pending inflight
  pending=$(pending_count)
  inflight=$(in_flight_count)

  echo "Queue: $pending offen | $inflight in-flight | pop $chunk → consume"

  if [[ "$dry" -eq 1 ]]; then
    head -n $((chunk + 1)) "$MISSING_TSV" | tail -n "$chunk" | while IFS=$'\t' read -r rel _; do
      [[ "$rel" == "relpath" || -z "$rel" ]] && continue
      echo "→ $rel"
    done
    echo "(dry-run — keine TSV-Änderung)"
    return
  fi

  while IFS= read -r rel; do
    [[ -z "$rel" ]] && continue
    src="$NAS_ROOT/$rel"
    dest_file="$dest/$rel"
    dest_dir=$(dirname "$dest_file")

    if [[ ! -f "$src" ]]; then
      echo "WARN: fehlt auf NAS: $src" >&2
      continue
    fi
    mkdir -p "$dest_dir"
    _prepare="${LEGACY_PREPARE_PDF:-/opt/paperless-scripts/legacy-prepare-pdf.sh}"
    if [[ -x "$_prepare" ]]; then
      "$_prepare" "$src" "$dest_file"
    else
      rsync -a "$src" "$dest_file"
    fi
    echo "→ $rel"
    copied=$((copied + 1))
  done < <(cmd_pop_missing "$chunk" "$dest")

  echo ""
  echo "copy-missing: $copied nach in-flight + consume | $(pending_count) offen | $(in_flight_count) in-flight"
}

cmd_import_chunk() {
  local batch="queue" chunk=20 dry=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --batch) batch="${2:?}"; shift 2 ;;
      --chunk) chunk="${2:?}"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
  done

  local before after dest="$CONSUME_ROOT/$batch"
  before=$(missing_count)
  [[ "$before" -gt 0 ]] || { echo "Fertig — Delta leer."; exit 0; }

  echo "=== import-chunk ($before fehlend, chunk=$chunk) ==="

  if [[ "$dry" -eq 1 ]]; then
    cmd_copy_missing --batch "$batch" --chunk "$chunk" --dry-run
    echo "(dry-run — kein warten/prune)"
    exit 0
  fi

  cmd_copy_missing --batch "$batch" --chunk "$chunk"
  wait_consume_empty "$dest"

  echo "Paperless-Checksums + in-flight reconcilen …"
  cmd_reconcile

  after=$(missing_count)
  echo ""
  echo "Chunk fertig: $before → $after fehlend ($(($before - after)) importiert)"
}

cmd_import_loop() {
  local n=0
  while true; do
    local left
    left=$(missing_count)
    [[ "$left" -le 0 ]] && break
    n=$((n + 1))
    echo ""
    echo "########## Chunk-Lauf #$n ($left fehlend) ##########"
    cmd_import_chunk "$@" || exit 1
  done
  echo ""
  echo "=== import-loop fertig ($n Chunks) ==="
}

cmd_missing() {
  [[ -f "$INVENTORY" ]] || { echo "FEHLER: kein Inventar — zuerst: $0 scan" >&2; exit 1; }
  [[ -f "$PL_CACHE" ]] || { echo "FEHLER: kein Paperless-Cache — zuerst: $0 fetch-paperless" >&2; exit 1; }
  INVENTORY="$INVENTORY" MISSING_TSV="$MISSING_TSV" NAS_ROOT="$NAS_ROOT" ENV_FILE="$ENV_FILE" \
    PL_CACHE="$PL_CACHE" REFRESH_PL=0 \
    run_python missing
}

cmd_all() {
  scan_nas "$@"
  echo ""
  cmd_summary
  cmd_vs_paperless
}

case "$CMD" in
  scan) scan_nas "$@" ;;
  summary) cmd_summary ;;
  duplicates) cmd_duplicates "$@" ;;
  vs-paperless) cmd_vs_paperless "$@" ;;
  fetch-paperless) cmd_fetch_paperless "$@" ;;
  missing) cmd_missing ;;
  prune-missing) REFRESH_PL=1 cmd_prune_missing ;;
  reconcile) cmd_reconcile ;;
  copy-missing) cmd_copy_missing "$@" ;;
  import-chunk) cmd_import_chunk "$@" ;;
  import-loop) cmd_import_loop "$@" ;;
  all) cmd_all "$@" ;;
  -h|--help|help) usage ;;
  *)
    echo "Unbekannter Befehl: $CMD" >&2
    usage
    exit 1
    ;;
esac
