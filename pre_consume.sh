#!/bin/bash
# Pre-Consume Skript für Paperless-NGX
# Schritt 1: PDF-Qualität via ocrmypdf verbessern
# Schritt 2: Swiss QR Bill Daten extrahieren (Sidecar JSON)
# Läuft auf CT 121

# ── Fail-Safe: bei Fehler sofort abbrechen ────────────────────────────────────
set -Eeuo pipefail
trap 'echo "[pre_consume] FEHLER in Zeile $LINENO — Exit $?" >&2' ERR

export TERM=xterm

# ── Konfiguration ─────────────────────────────────────────────────────────────
OCR_TIMEOUT="${PRE_CONSUME_OCR_TIMEOUT:-600}"   # 10 Minuten default
QR_TIMEOUT="${PRE_CONSUME_QR_TIMEOUT:-60}"      # 1 Minute default

file="$DOCUMENT_SOURCE_PATH"

if [ -z "$file" ]; then
    echo "[pre_consume] Fehler: DOCUMENT_SOURCE_PATH nicht gesetzt" >&2
    exit 1
fi

if [ ! -f "$file" ]; then
    echo "[pre_consume] Fehler: Datei nicht gefunden: $file" >&2
    exit 1
fi

echo "[pre_consume] Verarbeite: $file"

# ── Schritt 1: OCR-Optimierung via ocrmypdf ───────────────────────────────────
# tmp-File im gleichen Verzeichnis → atomisches mv garantiert (gleicher FS)
tmp_file="${file%.pdf}_ocr_tmp.pdf"

# Cleanup-Trap: tmp-File immer löschen bei Fehler
cleanup() {
    if [ -f "$tmp_file" ]; then
        rm -f "$tmp_file"
        echo "[pre_consume] Cleanup: tmp-File entfernt"
    fi
}
trap cleanup EXIT

if pdftotext "$file" - 2>/dev/null | grep -q "[a-zA-Z]"; then
    echo "[pre_consume] PDF enthält bereits OCR — führe --redo-ocr durch"
    timeout "$OCR_TIMEOUT" ocrmypdf \
        --redo-ocr \
        -l deu+ita+eng+fra \
        "$file" "$tmp_file"
else
    echo "[pre_consume] PDF ohne OCR — führe Bildoptimierung + OCR durch"
    timeout "$OCR_TIMEOUT" ocrmypdf \
        -l deu+ita+eng+fra \
        --optimize 2 \
        --deskew \
        --clean \
        --rotate-pages \
        --rotate-pages-threshold 7 \
        --output-type pdf \
        "$file" "$tmp_file"
fi

if [ ! -f "$tmp_file" ]; then
    echo "[pre_consume] Fehler: OCR-Ausgabe nicht erstellt" >&2
    exit 1
fi

# Atomisches Ersetzen (gleicher FS → mv ist atomic)
mv "$tmp_file" "$file"
echo "[pre_consume] Schritt 1 abgeschlossen: $file"

# Cleanup-Trap zurücksetzen (tmp ist weg)
trap - EXIT

# ── Schritt 2: Swiss QR Bill Parser ──────────────────────────────────────────
# QR-Fehler sind NICHT kritisch — dürfen nie die Ingestion blockieren
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
    # set +e: QR-Fehler NICHT als Fatal behandeln
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
