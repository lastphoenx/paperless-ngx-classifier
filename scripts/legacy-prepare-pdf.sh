#!/bin/bash
# OCR vor consume/ — Datei kommt stabil in Paperless (kein inotify-Requeue).
# Paperless 2.20 überspringt OCRmyPDF nur bei pdftotext >50 Zeichen.
set -euo pipefail

src="${1:?src pdf}"
dest="${2:?dest pdf}"
min="${LEGACY_MIN_TEXT_CHARS:-50}"
ocr_timeout="${LEGACY_OCR_TIMEOUT:-600}"
ocr_lang="${LEGACY_OCR_LANGUAGE:-deu+ita+eng+fra}"

mkdir -p "$(dirname "$dest")"
text="$(pdftotext "$src" - 2>/dev/null || true)"
if [[ ${#text} -gt $min ]]; then
    cp -a "$src" "$dest"
    echo "  copy (${#text} Zeichen): $(basename "$dest")"
    exit 0
fi

tmp="$(mktemp --tmpdir="${TMPDIR:-/tmp}" legacy-ocr.XXXXXX.pdf)"
trap 'rm -f "$tmp"' EXIT
echo "  ocr (<=${min} Zeichen): $(basename "$dest")"
set +e
timeout "$ocr_timeout" ocrmypdf -l "$ocr_lang" --output-type pdf "$src" "$tmp"
ex=$?
set -e
if [[ $ex -eq 0 || $ex -eq 6 ]]; then
    [[ $ex -eq 6 ]] && cp -a "$src" "$tmp"
    cp -a "$tmp" "$dest"
    text2="$(pdftotext "$dest" - 2>/dev/null || true)"
    echo "  → ${#text2} Zeichen nach OCR"
else
    echo "  WARN: ocrmypdf exit $ex — Original" >&2
    cp -a "$src" "$dest"
fi
