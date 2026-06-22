#!/usr/bin/env bash
# NAS-Altbestand: SHA256-Inventar, interne Dubletten, optional Abgleich mit Paperless-Checksums.
#
# Auf CT 121 (nach NFS-Mount):
#   ./scripts/legacy-nas-sha256.sh scan          # ~2300 PDFs, dauert einige Minuten
#   ./scripts/legacy-nas-sha256.sh summary       # Kurzstatistik
#   ./scripts/legacy-nas-sha256.sh duplicates    # Dubletten-Gruppen (TSV)
#   ./scripts/legacy-nas-sha256.sh vs-paperless  # erwartete Import-Dubletten
#   ./scripts/legacy-nas-sha256.sh all           # scan + summary + vs-paperless
#
set -euo pipefail

NAS_ROOT="${LEGACY_NAS_FINANZEN:-/mnt/nas-legacy/Eltern/Finanzen}"
STATE_DIR="${LEGACY_MIGRATE_STATE_DIR:-/mnt/paperless-data/legacy-migrate}"
ENV_FILE="${PAPERLESS_ENV:-/opt/paperless/.env}"
INVENTORY="${LEGACY_NAS_SHA256_TSV:-$STATE_DIR/nas-sha256.tsv}"
DUPES_TSV="${LEGACY_NAS_DUPES_TSV:-$STATE_DIR/nas-duplicates.tsv}"
SUMMARY_FILE="${LEGACY_NAS_SHA256_SUMMARY:-$STATE_DIR/nas-sha256-summary.txt}"
# Standard: Moni 2015/2016 aus Migration-Plan ausschliessen (| als Trenner)
EXCLUDE_REGEX="${LEGACY_NAS_EXCLUDE_REGEX:-Vorsorge/Moni/2015|Vorsorge/Moni/2016}"

CMD="${1:-summary}"
shift || true

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  echo ""
  echo "Befehle: scan | summary | duplicates | vs-paperless | all"
  echo "Optionen (scan): --refresh   alle Hashes neu berechnen"
  echo "Optionen (duplicates): --min N   nur Gruppen mit >= N Dateien (default 2)"
  echo ""
  echo "Ausgabe: $INVENTORY"
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
min_group = int(os.environ.get("MIN_GROUP", "2"))


def load_inventory(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        header = f.readline()
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            rel, size, mtime, sha = parts[0], parts[1], parts[2], parts[3]
            rows.append((rel, int(size), int(mtime), sha))
    return rows


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


def fetch_paperless_checksums():
    token = ""
    if os.path.isfile(env_file):
        for line in open(env_file, encoding="utf-8", errors="replace"):
            if line.startswith("PAPERLESS_TOKEN="):
                token = line.split("=", 1)[1].strip()
                break
    if not token:
        print("FEHLER: kein PAPERLESS_TOKEN in", env_file, file=sys.stderr)
        sys.exit(1)

    checksums = {}
    url = "http://127.0.0.1:8000/api/documents/?page_size=100&fields=id,checksum,title"
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Token {token}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
        for doc in data.get("results", []):
            cs = doc.get("checksum") or ""
            if cs:
                checksums[cs] = doc.get("id")
        url = data.get("next")
    return checksums


def vs_paperless(rows):
    by_hash, _, total, unique, dup_files, dup_groups, extra = analyze(rows)
    pl_checksums = fetch_paperless_checksums()

    nas_hashes = set(by_hash)
    pl_hashes = set(pl_checksums)

    overlap_hashes = nas_hashes & pl_hashes
    new_hashes = nas_hashes - pl_hashes

    files_already = sum(len(by_hash[h]) for h in overlap_hashes)
    files_new_content = sum(len(by_hash[h]) for h in new_hashes)

    lines = [
        "",
        "=== NAS vs. Paperless (Checksum-Abgleich) ===",
        f"Dokumente in Paperless (API):     {len(pl_checksums)}",
        f"Einzigartige NAS-Inhalte (SHA256): {unique}",
        f"Bereits in Paperless (Hash-Match): {len(overlap_hashes)} Hashes → {files_already} NAS-Dateien",
        f"Nicht in Paperless:              {len(new_hashes)} Hashes → {files_new_content} NAS-Dateien",
        "",
        "Erwartung beim Legacy-Import:",
        f"  ~{files_already} NAS-Dateien → Duplikat-Fehler (Inhalt schon in Paperless)",
        f"  ~{files_new_content} NAS-Dateien → könnten neue Docs werden (wenn importiert)",
        f"  Davon NAS-interne Kopien:        {dup_files} Dateien ({dup_groups} Gruppen)",
        f"  Max. neue Docs realistisch:      {len(new_hashes)} (ein Doc pro neuem Hash)",
        "",
        "Hinweis: Paperless checksum = SHA256 des Originals (wie dieses Inventar).",
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
  [[ -f "$INVENTORY" ]] || { echo "FEHLER: kein Inventar — zuerst: $0 scan" >&2; exit 1; }
  INVENTORY="$INVENTORY" SUMMARY_FILE="$SUMMARY_FILE" NAS_ROOT="$NAS_ROOT" ENV_FILE="$ENV_FILE" \
    run_python vs-paperless
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
  vs-paperless) cmd_vs_paperless ;;
  all) cmd_all "$@" ;;
  -h|--help|help) usage ;;
  *)
    echo "Unbekannter Befehl: $CMD" >&2
    usage
    exit 1
    ;;
esac
