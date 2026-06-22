#!/usr/bin/env bash
# NAS-Altbestand: SHA256-Inventar, interne Dubletten, optional Abgleich mit Paperless-Checksums.
#
# Auf CT 121 (nach NFS-Mount):
#   ./scripts/legacy-nas-sha256.sh scan          # ~2300 PDFs, dauert einige Minuten
#   ./scripts/legacy-nas-sha256.sh summary       # Kurzstatistik
#   ./scripts/legacy-nas-sha256.sh duplicates    # Dubletten-Gruppen (TSV)
#   ./scripts/legacy-nas-sha256.sh vs-paperless  # erwartete Import-Dubletten
#   ./scripts/legacy-nas-sha256.sh missing      # TSV: NAS-Pfade die in Paperless fehlen
#   ./scripts/legacy-nas-sha256.sh copy-missing --batch queue --chunk 20
#   ./scripts/legacy-nas-sha256.sh all           # scan + summary + vs-paperless
#
set -euo pipefail

NAS_ROOT="${LEGACY_NAS_FINANZEN:-/mnt/nas-legacy/Eltern/Finanzen}"
STATE_DIR="${LEGACY_MIGRATE_STATE_DIR:-/mnt/paperless-data/legacy-migrate}"
CONSUME_ROOT="${LEGACY_CONSUME_ROOT:-/mnt/paperless-data/consume/legacy}"
ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"
INVENTORY="${LEGACY_NAS_SHA256_TSV:-$STATE_DIR/nas-sha256.tsv}"
DUPES_TSV="${LEGACY_NAS_DUPES_TSV:-$STATE_DIR/nas-duplicates.tsv}"
MISSING_TSV="${LEGACY_NAS_MISSING_TSV:-$STATE_DIR/nas-missing-import.tsv}"
SUMMARY_FILE="${LEGACY_NAS_SHA256_SUMMARY:-$STATE_DIR/nas-sha256-summary.txt}"
# Standard: Moni 2015/2016 aus Migration-Plan ausschliessen (| als Trenner)
PL_CACHE="${LEGACY_PL_CHECKSUM_CACHE:-$STATE_DIR/paperless-checksums.tsv}"

CMD="${1:-summary}"
shift || true

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  echo ""
  echo "Befehle: scan | summary | duplicates | vs-paperless | fetch-paperless | missing | copy-missing | all"
  echo "Optionen (scan): --refresh   alle Hashes neu berechnen"
  echo "Optionen (vs-paperless|fetch-paperless): --refresh-paperless  Checksums neu von API"
  echo "Optionen (duplicates): --min N   nur Gruppen mit >= N Dateien (default 2)"
  echo "Optionen (copy-missing): --batch NAME  consume/legacy/NAME/ (default: queue)"
  echo "                         --chunk N     nur erste N fehlende PDFs"
  echo "                         --dry-run"
  echo ""
  echo "Ausgabe: $INVENTORY | missing: $MISSING_TSV"
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

  local dest="$CONSUME_ROOT/$batch"
  local n=0 copied=0
  local rel src dest_file dest_dir

  while IFS=$'\t' read -r rel _chk _copies _sha; do
    [[ "$rel" == "relpath" || -z "$rel" ]] && continue
    n=$((n + 1))
    [[ "$chunk" -gt 0 && "$copied" -ge "$chunk" ]] && break

    src="$NAS_ROOT/$rel"
    dest_file="$dest/$rel"
    dest_dir=$(dirname "$dest_file")

    if [[ ! -f "$src" ]]; then
      echo "WARN: fehlt auf NAS: $src" >&2
      continue
    fi
    if [[ -f "$dest_file" ]]; then
      echo "übersprungen (schon in consume): $rel"
      continue
    fi

    if [[ "$dry" -eq 1 ]]; then
      echo "→ $rel"
    else
      mkdir -p "$dest_dir"
      rsync -a "$src" "$dest_file"
      echo "→ $rel"
    fi
    copied=$((copied + 1))
  done <"$MISSING_TSV"

  echo ""
  echo "copy-missing: $copied PDFs nach $dest/ ($n in Liste, chunk=${chunk:-all}, dry=$dry)"
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
  copy-missing) cmd_copy_missing "$@" ;;
  all) cmd_all "$@" ;;
  -h|--help|help) usage ;;
  *)
    echo "Unbekannter Befehl: $CMD" >&2
    usage
    exit 1
    ;;
esac
