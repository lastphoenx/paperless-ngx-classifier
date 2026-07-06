#!/usr/bin/env python3
"""
Legacy QR-Split — PDF direkt auf dem Server testen (ohne Paperless/UI).

WICHTIG CT121: nicht /usr/bin/python3 — gleiches venv wie correspondent-manager:
  /opt/paperless-scripts/venv/bin/python3 legacy_qr_split_test.py /opt/scan.pdf --verbose-pages

QR-Inhalt: 6 Ziffern + Unterstrich, z. B. 010401_Lohn_Monika
Regex default: ^[0-9]{6}_[^\\s]+$

Abhängigkeiten: ./scripts/ensure-legacy-qr-deps.sh
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


def _find_scripts_root() -> Path:
    """Verzeichnis mit legacy_split_by_qr.py (deploy-flatten oder git clone)."""
    here = Path(__file__).resolve().parent
    for d in (here, here.parent, Path("/opt/paperless-scripts")):
        if (d / "legacy_split_by_qr.py").is_file():
            return d
    return here.parent


def _venv_pythons() -> list[Path]:
    root = _find_scripts_root()
    return [
        p for p in (
            Path("/opt/paperless-scripts/venv/bin/python3"),
            root / "venv/bin/python3",
            root.parent / "venv/bin/python3",
        )
        if p.is_file()
    ]


def _ensure_runtime_python() -> None:
    """System-python3 hat oft keine deps — automatisch venv nutzen."""
    if os.environ.get("LEGACY_QR_REEXEC"):
        return
    try:
        import pdf2image  # noqa: F401
        import pyzbar  # noqa: F401
        return
    except ImportError:
        pass
    me = Path(sys.executable).resolve()
    for vpy in _venv_pythons():
        if vpy.resolve() != me:
            os.environ["LEGACY_QR_REEXEC"] = "1"
            os.execv(str(vpy), [str(vpy), *sys.argv])
    print(
        "FEHLER: pdf2image/pyzbar fehlen.\n"
        "  sudo ./scripts/ensure-legacy-qr-deps.sh\n"
        "  /opt/paperless-scripts/venv/bin/python3 "
        f"{Path(__file__).name} …",
        file=sys.stderr,
    )
    sys.exit(1)


_ensure_runtime_python()

ROOT = _find_scripts_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from legacy_split_by_qr import (  # noqa: E402
    DEFAULT_QR_REGEX,
    _FALLBACK_DPIS,
    _RENDER_BACKENDS,
    _decode_page_image,
    _match_barcode,
    convert_pdf_pages,
    dump_rendered_page,
    find_split_markers,
    has_real_qr_splits,
    split_pdf_at_markers,
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
    log.info("Module: %s", ROOT)
    for cmd in ("pdftoppm", "pdftocairo", "gs", "zbarimg"):
        path = shutil.which(cmd)
        extra = ""
        if cmd == "gs" and not path:
            extra = " — FEHLT (Original-Script nutzte Ghostscript 600 dpi)"
        elif cmd == "zbarimg" and not path:
            extra = " (pyzbar reicht, zbarimg optional)"
        log.info("%s: %s%s", cmd, path or "NICHT GEFUNDEN", extra)
    try:
        import pyzbar  # noqa: F401
        log.info("pyzbar: ok")
    except ImportError:
        log.error("pyzbar: FEHLT")
    try:
        import pdf2image  # noqa: F401
        log.info("pdf2image: ok")
    except ImportError:
        log.error("pdf2image: FEHLT")
    zbar_so = Path("/usr/lib/x86_64-linux-gnu/libzbar.so.0")
    if not zbar_so.exists():
        zbar_so = Path("/usr/lib/libzbar.so.0")
    log.info("libzbar: %s", "ok" if zbar_so.exists() else "FEHLT — apt install libzbar0")


def _verbose_page_scan(
    pdf_path: Path,
    regex: str,
    log: logging.Logger,
) -> None:
    from pypdf import PdfReader

    pattern = re.compile(regex)
    total = len(PdfReader(str(pdf_path)).pages)
    log.info("=== Seiten-Scan (verbose) ===")
    log.info("PDF: %s (%d Seiten, %s bytes)", pdf_path, total, pdf_path.stat().st_size)
    log.info("Backends: %s | DPIs: %s", ", ".join(_RENDER_BACKENDS), list(_FALLBACK_DPIS))

    found_any = False
    for backend in _RENDER_BACKENDS:
        if found_any:
            break
        for try_dpi in _FALLBACK_DPIS:
            log.info("--- %s @ DPI %d ---", backend, try_dpi)
            t0 = time.monotonic()
            try:
                images = convert_pdf_pages(str(pdf_path), dpi=try_dpi, backend=backend)
            except Exception as e:
                log.error("Render fehlgeschlagen: %s", e)
                continue
            log.info("Gerendert: %d Seiten in %.1fs", len(images), time.monotonic() - t0)
            if len(images) != total:
                log.warning("Seitenanzahl Render (%d) != PDF (%d)", len(images), total)

            any_code = False
            for page_num, image in enumerate(images, start=1):
                t1 = time.monotonic()
                try:
                    raw_list = _decode_page_image(image)
                except Exception as e:
                    log.error("  S.%d: Decode-Fehler: %s", page_num, e)
                    continue
                dt = time.monotonic() - t1
                if not raw_list:
                    log.info("  S.%d: (kein QR) [%.2fs]", page_num, dt)
                    continue
                any_code = True
                for raw in raw_list:
                    hit = _match_barcode(raw, pattern)
                    mark = "MATCH" if hit else "kein Match"
                    show = hit or raw
                    log.info("  S.%d: [%s] «%s» [%.2fs]", page_num, mark, show[:120], dt)
            if any_code:
                log.info("%s @ %d dpi: Code(s) gefunden", backend, try_dpi)
                found_any = True
                break
            log.info("%s @ %d dpi: nichts erkannt", backend, try_dpi)


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
    parser = argparse.ArgumentParser(description="Legacy QR-Split: lokales PDF testen")
    parser.add_argument("pdf", type=Path, help="Pfad zur PDF-Datei")
    parser.add_argument(
        "--regex",
        default=os.environ.get("LEGACY_SPLIT_QR_REGEX", DEFAULT_QR_REGEX),
    )
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--split", action="store_true")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(os.environ.get("PAPERLESS_CONSUME_DIR", "/mnt/paperless-data/consume")),
    )
    parser.add_argument("--verbose-pages", action="store_true")
    parser.add_argument(
        "--dump-page",
        type=int,
        default=0,
        metavar="N",
        help="Seite N als PNG rendern (Debug: QR im Raster?) — bricht nach Dump ab",
    )
    parser.add_argument(
        "--dump-backend",
        choices=_RENDER_BACKENDS,
        default="ghostscript",
        help="Renderer für --dump-page (default: ghostscript wie Bash-Original)",
    )
    parser.add_argument(
        "--dump-dpi",
        type=int,
        default=150,
        help="DPI für --dump-page (default: 150)",
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

    if args.dump_page > 0:
        out_png = pdf_path.parent / f"{pdf_path.stem}_page{args.dump_page}_{args.dump_backend}_{args.dump_dpi}dpi.png"
        try:
            dump_rendered_page(
                str(pdf_path), args.dump_page, out_png,
                dpi=args.dump_dpi, backend=args.dump_backend,
            )
        except Exception as e:
            log.exception("Dump fehlgeschlagen: %s", e)
            return 1
        log.info("PNG: %s (%s bytes)", out_png, out_png.stat().st_size)
        try:
            images = convert_pdf_pages(
                str(pdf_path), dpi=args.dump_dpi, backend=args.dump_backend,
                first_page=args.dump_page, last_page=args.dump_page,
            )
            raw_list = _decode_page_image(images[0]) if images else []
            if raw_list:
                log.info("Decode auf Dump: %s", raw_list)
            else:
                log.warning("Decode auf Dump: kein QR — PNG prüfen (Smartphone/zbarimg)")
        except Exception as e:
            log.warning("Decode auf Dump: %s", e)
        return 0

    if args.verbose_pages:
        _verbose_page_scan(pdf_path, args.regex, log)

    log.info("=== find_split_markers ===")
    t0 = time.monotonic()
    try:
        markers, total, qr_debug = find_split_markers(str(pdf_path), regex=args.regex)
    except Exception as e:
        log.exception("Scan fehlgeschlagen: %s", e)
        return 1
    log.info("Dauer: %.1fs", time.monotonic() - t0)
    log.info(
        "Seiten: %d | Marker: %d | QR gelesen: %d | Regex-Treffer: %d",
        total, len(markers), len(qr_debug),
        sum(1 for x in qr_debug if x.get("matched")),
    )

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
        result["splits"].append({"barcode": barcode, "from_page": from_page, "to_page": to_page})

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
        log.info("Vorschau OK — Split: ... --split --out-dir %s", args.out_dir)
    else:
        log.error("Kein Split möglich — kein QR passend zu Regex.")

    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log.info("JSON: %s", args.json)

    log.info("=== Ende (Log: %s) ===", log_path)
    return 0 if has_real_qr_splits(markers) else 2


if __name__ == "__main__":
    raise SystemExit(main())
