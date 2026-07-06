"""
Legacy QR-Split — Port von tsa_barcode_split_function.sh (zbar + Regex).

Pro Seite QR lesen; Treffer ^[0-9]{6}_…$ = Start eines Teildokuments.
Ausgabe: ocrscan_{barcode}_{basename}_p{von}_bis_p{bis}.pdf
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("legacy_split_by_qr")

DEFAULT_QR_REGEX = r"^[0-9]{6}_[^\s]+$"
DEFAULT_DPI = 600  # wie tsa_barcode_split_function.sh (Ghostscript 600dpi)
_FALLBACK_DPIS = (600, 400, 300)


def _decode_barcodes_from_image_path(img_path: str) -> list[str]:
    """Alle lesbaren Barcode-Texte von PNG (pyzbar, alle Typen)."""
    proc = subprocess.run(
        [
            sys.executable, "-c",
            "import sys,json; from pyzbar.pyzbar import decode; "
            "from PIL import Image; "
            f"img=Image.open({img_path!r}); "
            "codes=[]; "
            "for b in decode(img): "
            "  try: t=b.data.decode('utf-8','replace').strip() "
            "  except Exception: "
            "    try: t=b.data.decode('latin-1','replace').strip() "
            "    except Exception: continue "
            "  if t: codes.append(t); "
            "print(json.dumps(codes))",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode == 0:
        import json
        try:
            return [s for s in json.loads(proc.stdout.strip() or "[]") if s.strip()]
        except json.JSONDecodeError:
            pass
    return _zbarimg_decode(img_path)


def _zbarimg_decode(img_path: str) -> list[str]:
    """Fallback wie tsa_barcode_split_function.sh (zbarimg)."""
    if not shutil.which("zbarimg"):
        return []
    proc = subprocess.run(
        ["zbarimg", "-q", "--raw", img_path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode not in (0, 4):  # 4 = found codes
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def _enhance_page_image(image):
    """Kontrast für schwache QR auf Scan/Archiv-PDF."""
    from PIL import ImageOps
    return ImageOps.autocontrast(image.convert("L"))


def scan_page_qrs(
    pdf_path: str,
    page_num: int,
    *,
    dpi: int = DEFAULT_DPI,
    dpis: tuple[int, ...] | None = None,
) -> list[str]:
    """Alle Barcode-Texte auf Seite page_num (1-basiert), mehrere DPI-Versuche."""
    try:
        from pdf2image import convert_from_path
    except ImportError:
        raise RuntimeError("pdf2image fehlt — pip install pdf2image") from None

    tried = dpis or _FALLBACK_DPIS if dpi == DEFAULT_DPI else (dpi,)
    seen: list[str] = []
    seen_set: set[str] = set()

    for try_dpi in tried:
        images = convert_from_path(
            pdf_path, dpi=try_dpi, first_page=page_num, last_page=page_num,
        )
        if not images:
            continue
        for variant in (images[0], _enhance_page_image(images[0])):
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tmp = tf.name
            try:
                variant.save(tmp, format="PNG")
                for raw in _decode_barcodes_from_image_path(tmp):
                    if raw not in seen_set:
                        seen_set.add(raw)
                        seen.append(raw)
            finally:
                Path(tmp).unlink(missing_ok=True)
        if seen:
            break
    return seen


def scan_page_qr(pdf_path: str, page_num: int, *, dpi: int = DEFAULT_DPI) -> str | None:
    """Erste Barcode-Zeichenkette auf Seite page_num (1-basiert) oder None."""
    found = scan_page_qrs(pdf_path, page_num, dpi=dpi)
    return found[0] if found else None


def _match_barcode(text: str, pattern: re.Pattern[str]) -> str | None:
    text = (text or "").strip()
    if pattern.match(text):
        return text
    m = re.search(r"(\d{6}_[^\s]+)", text)
    if m and pattern.match(m.group(1)):
        return m.group(1)
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
        page_hits: list[str] = []
        for raw in scan_page_qrs(pdf_path, page, dpi=dpi):
            hit = _match_barcode(raw, pattern)
            qr_debug.append({"page": page, "raw": raw, "matched": bool(hit)})
            if hit and hit not in page_hits:
                page_hits.append(hit)
        for hit in page_hits:
            if markers and markers[-1] == (hit, page):
                continue
            log.info("QR Seite %d: %s", page, hit)
            markers.append((hit, page))

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
            f.flush()
            os.fsync(f.fileno())

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
