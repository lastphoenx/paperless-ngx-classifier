"""
Legacy QR-Split — Port von tsa_barcode_split_function.sh (zbar + Regex).

Pro Seite QR lesen; Treffer ^[0-9]{6}_…$ = Start eines Teildokuments.
Ausgabe: ocrscan_{barcode}_{basename}_p{von}_bis_p{bis}.pdf
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("legacy_split_by_qr")

DEFAULT_QR_REGEX = r"^[0-9]{6}_[^\s]+$"
DEFAULT_DPI = 600  # wie tsa_barcode_split_function.sh (Ghostscript 600dpi)


def _decode_qr_from_image_path(img_path: str) -> list[str]:
    """QR-Texte von PNG (pyzbar im Subprocess — stabiler als inline)."""
    proc = subprocess.run(
        [
            sys.executable, "-c",
            "import sys,json; from pyzbar.pyzbar import decode; "
            "from PIL import Image; "
            f"img=Image.open({img_path!r}); "
            "codes=[b.data.decode('utf-8','replace') for b in decode(img) "
            "if b.type in ('QRCODE','QR')]; "
            "print(json.dumps(codes))",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return []
    import json

    try:
        return json.loads(proc.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return []


def scan_page_qrs(pdf_path: str, page_num: int, *, dpi: int = DEFAULT_DPI) -> list[str]:
    """Alle QR-Texte auf Seite page_num (1-basiert)."""
    try:
        from pdf2image import convert_from_path
    except ImportError:
        raise RuntimeError("pdf2image fehlt — pip install pdf2image") from None

    images = convert_from_path(
        pdf_path, dpi=dpi, first_page=page_num, last_page=page_num,
    )
    if not images:
        return []

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp = tf.name
    try:
        images[0].save(tmp, format="PNG")
        return [s for s in (_decode_qr_from_image_path(tmp)) if s.strip()]
    finally:
        Path(tmp).unlink(missing_ok=True)


def scan_page_qr(pdf_path: str, page_num: int, *, dpi: int = DEFAULT_DPI) -> str | None:
    """Erste QR-Zeichenkette auf Seite page_num (1-basiert) oder None."""
    found = scan_page_qrs(pdf_path, page_num, dpi=dpi)
    return found[0] if found else None


def _match_barcode(text: str, pattern: re.Pattern[str]) -> str | None:
    text = (text or "").strip()
    if pattern.match(text):
        return text
    return None


def _basename_stem(filename: str) -> str:
    name = Path(filename).name
    if name.lower().startswith("scan_"):
        name = name[5:]
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name


def _doc_name_from_barcode(barcode: str) -> str:
    return re.sub(r"\s+", "_", barcode.replace("/", "_"))


def find_split_markers(
    pdf_path: str,
    *,
    regex: str = DEFAULT_QR_REGEX,
    dpi: int = DEFAULT_DPI,
) -> tuple[list[tuple[str, int]], int, list[dict]]:
    """
    [(barcode, start_page), …] — 1-basierte Startseiten.
    Optional «Kein_Barcode» ab S.1 wenn erster Treffer später kommt (wie Bash-Script).
    Returns: (markers, total_pages, qr_debug) — qr_debug: [{page, raw, matched}]
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf fehlt — pip install pypdf") from None

    total = len(PdfReader(pdf_path).pages)
    pattern = re.compile(regex)
    markers: list[tuple[str, int]] = []
    qr_debug: list[dict] = []

    for page in range(1, total + 1):
        for raw in scan_page_qrs(pdf_path, page, dpi=dpi):
            hit = _match_barcode(raw, pattern)
            qr_debug.append({"page": page, "raw": raw, "matched": bool(hit)})
            if hit:
                log.info("QR Seite %d: %s", page, hit)
                markers.append((hit, page))
                break

    if not markers:
        return [], total, qr_debug

    if markers[0][1] > 1:
        log.info("Erster QR auf S.%d — Prefix Kein_Barcode S.1", markers[0][1])
        markers.insert(0, ("Kein_Barcode", 1))

    return markers, total, qr_debug


def has_real_qr_splits(markers: list[tuple[str, int]]) -> bool:
    """Mindestens ein echter Barcode (nicht nur Kein_Barcode über alles)."""
    return any(name != "Kein_Barcode" for name, _ in markers)


def split_pdf_by_qr(
    pdf_path: str,
    output_dir: str | Path,
    *,
    regex: str = DEFAULT_QR_REGEX,
    dpi: int = DEFAULT_DPI,
    source_basename: str | None = None,
) -> list[dict]:
    """
    Splittet PDF an QR-Markern. Returns Liste:
    {barcode, from_page, to_page, filename, path}
    """
    from pypdf import PdfReader, PdfWriter

    pdf_path = str(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    markers, total = find_split_markers(pdf_path, regex=regex, dpi=dpi)[:2]
    if not has_real_qr_splits(markers):
        return []

    f_stem = _basename_stem(source_basename or Path(pdf_path).name)
    reader = PdfReader(pdf_path)
    results: list[dict] = []

    for i, (barcode, from_page) in enumerate(markers):
        if i + 1 < len(markers):
            to_page = markers[i + 1][1] - 1
        else:
            to_page = total
        doc_name = _doc_name_from_barcode(barcode)
        filename = f"ocrscan_{doc_name}_{f_stem}_p{from_page}_bis_p{to_page}.pdf"
        out_path = output_dir / filename

        writer = PdfWriter()
        for p in range(from_page - 1, to_page):
            writer.add_page(reader.pages[p])
        with open(out_path, "wb") as f:
            writer.write(f)

        results.append({
            "barcode": barcode,
            "from_page": from_page,
            "to_page": to_page,
            "filename": filename,
            "path": str(out_path),
        })
        log.info("Split: %s (S. %d–%d)", filename, from_page, to_page)

    return results


def split_paperless_document(
    pdf_bytes: bytes,
    output_dir: str | Path,
    *,
    original_filename: str = "document.pdf",
    regex: str = DEFAULT_QR_REGEX,
    dpi: int = DEFAULT_DPI,
) -> list[dict]:
    """PDF-Bytes (z. B. aus Paperless) splitten und in output_dir schreiben."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_bytes)
        tmp_pdf = tf.name
    try:
        return split_pdf_by_qr(
            tmp_pdf, output_dir,
            regex=regex, dpi=dpi, source_basename=original_filename,
        )
    finally:
        Path(tmp_pdf).unlink(missing_ok=True)
