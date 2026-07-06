#!/usr/bin/env python3
"""
Legacy QR-Split — PDF direkt auf dem Server testen (ohne Paperless/UI).

QR-Inhalt erwartet: 6 Ziffern + Unterstrich + Text, z. B. 010401_Lohn_Monika
Regex default: ^[0-9]{6}_[^\\s]+$

Beispiele (auf CT121):
  cd /opt/paperless-scripts
  python3 scripts/legacy_qr_split_test.py /pfad/zum/scan.pdf

  python3 scripts/legacy_qr_split_test.py scan651.pdf --log /tmp/qr651.log

  python3 scripts/legacy_qr_split_test.py scan651.pdf --split \\
      --out-dir /mnt/paperless-data/consume

  LEGACY_SPLIT_QR_REGEX='^[0-9]{6}_.+$' python3 scripts/legacy_qr_split_test.py scan.pdf
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legacy_split_by_qr import (  # noqa: E402
    DEFAULT_QR_REGEX,
    _FALLBACK_DPIS,
    _decode_page_image,
    _match_barcode,
    find_split_markers,
    has_real_qr_splits,
    split_pdf_at_markers,
    split_pdf_by_qr,
)


def _setup_logging(log_path: Path | None) -> logging.Logger:
    log = logging.getLogger("legacy_qr_split_test")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


def _check_deps(log: logging.Logger) -> None:
    log.info("=== Umgebung ===")
    log.info("Python: %s", sys.executable)
    log.info("Repo:   %s", ROOT)
    for cmd in ("pdftoppm", "zbarimg"):
        path = shutil.which(cmd)
        log.info("%s: %s", cmd, path or "NICHT GEFUNDEN")
    try:
        import pyzbar  # noqa: F401
        log.info("pyzbar: ok (%s)", pyzbar.__file__)
    except ImportError:
        log.warning("pyzbar: FEHLT — pip install pyzbar")
    try:
        import pdf2image  # noqa: F401
        log.info("pdf2image: ok")
    except ImportError:
        log.warning("pdf2image: FEHLT")


def _verbose_page_scan(
    pdf_path: Path,
    regex: str,
    log: logging.Logger,
) -> None:
    """Pro Seite / DPI: alle gelesenen QR-Texte + Regex-Treffer."""
    from pdf2image import convert_from_path
    from pypdf import PdfReader

    pattern = re.compile(regex)
    total = len(PdfReader(str(pdf_path)).pages)
    log.info("=== Seiten-Scan (verbose) ===")
    log.info("PDF: %s (%d Seiten, %s bytes)", pdf_path, total, pdf_path.stat().st_size)

    for try_dpi in _FALLBACK_DPIS:
        log.info("--- DPI %d ---", try_dpi)
        t0 = time.monotonic()
        try:
            images = convert_from_path(str(pdf_path), dpi=try_dpi)
        except Exception as e:
            log.error("Render fehlgeschlagen: %s", e)
            continue
        log.info("Gerendert: %d Seiten in %.1fs", len(images), time.monotonic() - t0)
        if len(images) != total:
            log.warning("Seitenanzahl Render (%d) != PDF (%d)", len(images), total)

        any_code = False
        for page_num, image in enumerate(images, start=1):
            t1 = time.monotonic()
            raw_list = _decode_page_image(image)
            dt = time.monotonic() - t1
            if not raw_list:
                log.info("  S.%d: (kein QR/barcode) [%.2fs]", page_num, dt)
                continue
            any_code = True
            for raw in raw_list:
                hit = _match_barcode(raw, pattern)
                mark = "MATCH" if hit else "kein Match"
                show = hit or raw
                log.info("  S.%d: [%s] «%s» [%.2fs]", page_num, mark, show[:120], dt)
        if any_code:
            log.info("DPI %d: mindestens ein Code gefunden — höhere DPI übersprungen", try_dpi)
            break
        log.info("DPI %d: nichts erkannt", try_dpi)


def _print_split_plan(
    markers: list[tuple[str, int]],
    total: int,
    log: logging.Logger,
) -> None:
    log.info("=== Split-Plan ===")
    if not markers:
        log.info("(keine Marker)")
        return
    for i, (barcode, from_page) in enumerate(markers):
        to_page = markers[i + 1][1] - 1 if i + 1 < len(markers) else total
        log.info("  Teil %d: S.%d–%d  →  %s", i + 1, from_page, to_page, barcode)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Legacy QR-Split: lokales PDF testen mit Protokoll",
    )
    parser.add_argument("pdf", type=Path, help="Pfad zur PDF-Datei")
    parser.add_argument(
        "--regex",
        default=os.environ.get("LEGACY_SPLIT_QR_REGEX", DEFAULT_QR_REGEX),
        help=f"Regex für QR-Inhalt (default: {DEFAULT_QR_REGEX})",
    )
    parser.add_argument(
        "--log", type=Path, default=None,
        help="Zusätzlich in Log-Datei schreiben",
    )
    parser.add_argument(
        "--json", type=Path, default=None,
        help="Ergebnis als JSON schreiben",
    )
    parser.add_argument(
        "--split", action="store_true",
        help="PDF wirklich splitten (default: nur Vorschau)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(os.environ.get("PAPERLESS_CONSUME_DIR", "/mnt/paperless-data/consume")),
        help="Ziel bei --split",
    )
    parser.add_argument(
        "--verbose-pages", action="store_true",
        help="Jede Seite / DPI einzeln protokollieren",
    )
    args = parser.parse_args()

    pdf_path = args.pdf.expanduser().resolve()
    if not pdf_path.is_file():
        print(f"Datei nicht gefunden: {pdf_path}", file=sys.stderr)
        return 1

    log_path = args.log
    if log_path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = pdf_path.parent / f"legacy_qr_split_{pdf_path.stem}_{ts}.log"

    log = _setup_logging(log_path)
    log.info("Legacy QR-Split Test — %s", datetime.now(timezone.utc).isoformat())
    log.info("Log-Datei: %s", log_path)

    _check_deps(log)
    log.info("Regex: %s", args.regex)

    if args.verbose_pages:
        _verbose_page_scan(pdf_path, args.regex, log)

    log.info("=== find_split_markers ===")
    t0 = time.monotonic()
    markers, total, qr_debug = find_split_markers(str(pdf_path), regex=args.regex)
    log.info("Dauer: %.1fs", time.monotonic() - t0)
    log.info("Seiten: %d | Marker: %d | QR gelesen: %d | Regex-Treffer: %d",
             total, len(markers), len(qr_debug),
             sum(1 for x in qr_debug if x.get("matched")))

    for entry in qr_debug:
        raw = str(entry.get("raw", ""))[:120]
        flag = "OK" if entry.get("matched") else "—"
        log.info("  qr_debug S.%s [%s] «%s»", entry.get("page"), flag, raw)

    _print_split_plan(markers, total, log)

    result = {
        "pdf": str(pdf_path),
        "pages": total,
        "regex": args.regex,
        "markers": [{"barcode": b, "page": p} for b, p in markers],
        "qr_debug": qr_debug,
        "split_possible": has_real_qr_splits(markers),
        "splits": [],
        "output_files": [],
    }

    for i, (barcode, from_page) in enumerate(markers):
        to_page = markers[i + 1][1] - 1 if i + 1 < len(markers) else total
        result["splits"].append({
            "barcode": barcode, "from_page": from_page, "to_page": to_page,
        })

    if args.split:
        if not has_real_qr_splits(markers):
            log.error("Split abgebrochen — keine passenden QR-Marker")
            return 2
        log.info("=== Schreibe nach %s ===", args.out_dir)
        parts = split_pdf_at_markers(
            str(pdf_path), args.out_dir, markers, total,
            source_basename=pdf_path.name,
        )
        for p in parts:
            log.info("  %s", p["filename"])
            result["output_files"].append(p)
    elif has_real_qr_splits(markers):
        log.info("Vorschau OK — zum Splitten: ... --split --out-dir %s", args.out_dir)
    else:
        log.error(
            "Kein Split möglich — kein QR passend zu Regex. "
            "Tipp: --verbose-pages oder Original-PDF von NAS testen."
        )

    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log.info("JSON: %s", args.json)

    log.info("=== Ende (Log: %s) ===", log_path)
    return 0 if has_real_qr_splits(markers) else 2


if __name__ == "__main__":
    raise SystemExit(main())
