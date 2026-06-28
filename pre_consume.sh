#!/bin/bash
# Pre-Consume Skript für Paperless-NGX
# VERSION: 1.3 — Legacy: OCR nur bei Scans ohne Textschicht (Paperless 50-Zeichen-Grenze)
# Schritt 1: PDF-Qualität via ocrmypdf verbessern
# Schritt 2: Swiss QR Bill Daten extrahieren (Sidecar JSON)
# Läuft auf CT 121
# ── Fail-Safe: bei Fehler sofort abbrechen ────────────────────────────────────
set -Eeuo pipefail
export TERM=xterm

# Fehler-Trap — nur aktiv wenn _OCR_OK nicht gesetzt
_OCR_OK=0
trap '[[ $_OCR_OK -eq 0 ]] && echo "[pre_consume] FEHLER in Zeile $LINENO — Exit $?" >&2' ERR

# ── Konfiguration ─────────────────────────────────────────────────────────────
OCR_TIMEOUT="${PRE_CONSUME_OCR_TIMEOUT:-600}"
QR_TIMEOUT="${PRE_CONSUME_QR_TIMEOUT:-60}"
file="$DOCUMENT_SOURCE_PATH"
if [ -z "$file" ]; then
    echo "[pre_consume] Fehler: DOCUMENT_SOURCE_PATH nicht gesetzt" >&2; exit 1
fi
if [ ! -f "$file" ]; then
    echo "[pre_consume] Fehler: Datei nicht gefunden: $file" >&2; exit 1
fi
echo "[pre_consume] Verarbeite: $file"

# ── Legacy-Altbestand: keine OCR / kein QR (nur Paperless-Indexierung) ────────
_legacy_markers="${LEGACY_CONSUME_MARKERS:-/legacy/}"
_file_lc="${file//\\//}"
_file_lc="${_file_lc,,}"
IFS=',' read -r -a _markers <<< "$_legacy_markers"
for _m in "${_markers[@]}"; do
    _m="${_m#"${_m%%[![:space:]]*}"}"
    _m="${_m%"${_m##*[![:space:]]}"}"
    [[ -z "$_m" ]] && continue
    if [[ "$_file_lc" == *"${_m,,}"* ]]; then
        _marker_dir="/tmp/paperless_legacy_markers"
        mkdir -p "$_marker_dir"
        printf '%s' "$file" > "$_marker_dir/$(basename "$file")"

        # Paperless 2.20: PAPERLESS_OCR_SKIP_ARCHIVE_FILE=always überspringt OCRmyPDF nur,
        # wenn pdftotext >50 Zeichen liefert. Reine Bild-Scans → volles OCR inkl. PDF/A.
        _legacy_min="${LEGACY_MIN_TEXT_CHARS:-50}"
        _text="$(pdftotext "$file" - 2>/dev/null || true)"
        if [[ ${#_text} -gt $_legacy_min ]]; then
            echo "[pre_consume] Legacy-Import — Text vorhanden (${#_text} Zeichen), übersprungen ($file)"
            exit 0
        fi

        echo "[pre_consume] Legacy-Import — Scan ohne Textschicht (<=${_legacy_min} Zeichen), OCR ($file)"
        _tmp="${file%.pdf}_legacy_ocr_tmp.pdf"
        set +e
        timeout "$OCR_TIMEOUT" ocrmypdf \
            -l deu+ita+eng+fra \
            --output-type pdf \
            "$file" "$_tmp"
        _ocr_exit=$?
        set -e
        case $_ocr_exit in
            0)   mv "$_tmp" "$file"
                 echo "[pre_consume] Legacy-OCR abgeschlossen: $file" ;;
            6)   rm -f "$_tmp"
                 echo "[pre_consume] Legacy-OCR übersprungen (Exit 6), Original beibehalten" ;;
            *)   rm -f "$_tmp"
                 echo "[pre_consume] Legacy-OCR Fehler: Exit $_ocr_exit" >&2
                 exit 1 ;;
        esac
        exit 0
    fi
done

# ── Pipeline-Lock (shared mit post_consume.py — serialisiert OCR bei Parallel-Workern) ──
PIPELINE_LOCK="/tmp/paperless_consume_pipeline.lock"
exec 200>"$PIPELINE_LOCK"
echo "[pre_consume] Warte auf Pipeline-Lock..."
if ! flock -x -w 1800 200; then
    echo "[pre_consume] FEHLER: Pipeline-Lock Timeout (1800s)" >&2
    exit 1
fi
echo "[pre_consume] Pipeline-Lock erhalten"

# Waisen: qr_meta.json ohne zugehöriges PDF (Split-Läufe / abgebrochene Consumes)
consume_dir="$(dirname "$file")"
for orphan in "$consume_dir"/*_qr_meta.json; do
    [ -f "$orphan" ] || continue
    base="${orphan%_qr_meta.json}"
    if [ ! -f "${base}.pdf" ]; then
        rm -f "$orphan"
        echo "[pre_consume] Waise entfernt: $(basename "$orphan")"
    fi
done

# Waisen aus früheren Läufen (post_consume hat Sidecar nicht gelöscht / PDF weg)
qr_meta="${file%.pdf}_qr_meta.json"
if [ -f "$qr_meta" ]; then
    rm -f "$qr_meta"
    echo "[pre_consume] Altes qr_meta-Sidecar entfernt ($qr_meta)"
fi

# ── Schritt 1: OCR-Optimierung via ocrmypdf ───────────────────────────────────
tmp_file="${file%.pdf}_ocr_tmp.pdf"
cleanup() {
    if [ -f "$tmp_file" ]; then
        rm -f "$tmp_file"
        echo "[pre_consume] Cleanup: tmp-File entfernt"
    fi
    local qr_meta="${file%.pdf}_qr_meta.json"
    if [ -f "$qr_meta" ]; then
        rm -f "$qr_meta"
        echo "[pre_consume] Cleanup: qr_meta entfernt ($qr_meta)"
    fi
}
trap cleanup EXIT

if pdftotext "$file" - 2>/dev/null | grep -q "[a-zA-Z]"; then
    echo "[pre_consume] PDF enthält bereits OCR — führe --redo-ocr durch"
    set +e
    timeout "$OCR_TIMEOUT" ocrmypdf \
        --redo-ocr \
        -l deu+ita+eng+fra \
        "$file" "$tmp_file"
    OCR_EXIT=$?
    set -e
    case $OCR_EXIT in
        0)   echo "[pre_consume] OCR --redo-ocr abgeschlossen" ;;
        6)   _OCR_OK=1
             echo "[pre_consume] PDF bereits optimal (PriorOcrFoundError) — OCR übersprungen, Original beibehalten"
             rm -f "$tmp_file"
             trap - EXIT
             ;;
        *)   echo "[pre_consume] OCR Fehler: Exit $OCR_EXIT" >&2
             exit 1
             ;;
    esac
else
    echo "[pre_consume] PDF ohne OCR — führe Bildoptimierung + OCR durch"
    set +e
    timeout "$OCR_TIMEOUT" ocrmypdf \
        -l deu+ita+eng+fra \
        --optimize 2 \
        --deskew \
        --clean \
        --rotate-pages \
        --rotate-pages-threshold 7 \
        --output-type pdf \
        "$file" "$tmp_file"
    OCR_EXIT=$?
    set -e
    case $OCR_EXIT in
        0)   echo "[pre_consume] OCR abgeschlossen" ;;
        6)   _OCR_OK=1
             echo "[pre_consume] PDF bereits optimal (Exit 6) — OCR übersprungen, Original beibehalten"
             rm -f "$tmp_file"
             trap - EXIT
             ;;
        *)   echo "[pre_consume] OCR Fehler: Exit $OCR_EXIT" >&2
             exit 1
             ;;
    esac
fi

if [ -f "$tmp_file" ]; then
    mv "$tmp_file" "$file"
    echo "[pre_consume] Schritt 1 abgeschlossen: $file"
    trap - EXIT
fi

# ── Schritt 2: Swiss QR Bill Parser ──────────────────────────────────────────
QR_SCRIPT="/opt/paperless-scripts/pre_consume_qr.py"
VENV_PYTHON="/opt/paperless-scripts/venv/bin/python3"
if [ -f "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    echo "[pre_consume] Python nicht gefunden — QR-Scan übersprungen"
    exit 0
fi
if [ -f "$QR_SCRIPT" ]; then
    echo "[pre_consume] Schritt 2: QR-Scan startet (timeout ${QR_TIMEOUT}s)"
    set +e
    timeout "$QR_TIMEOUT" "$PYTHON" "$QR_SCRIPT"
    QR_EXIT=$?
    set -e
    case $QR_EXIT in
        0)   echo "[pre_consume] Schritt 2: QR-Scan abgeschlossen" ;;
        124) echo "[pre_consume] Schritt 2: QR-Scan Timeout (${QR_TIMEOUT}s) — ignoriert" ;;
        *)   echo "[pre_consume] Schritt 2: QR-Scan fehlgeschlagen (exit $QR_EXIT) — ignoriert" ;;
    esac
else
    echo "[pre_consume] $QR_SCRIPT nicht gefunden — QR-Scan übersprungen"
fi
echo "[pre_consume] Fertig: $file"
exit 0
