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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger("legacy_split_by_qr")

DEFAULT_QR_REGEX = r"^[0-9]{6}_[^\s]+$"
DEFAULT_DPI = 400  # 400 zuerst (schneller); 600 als Fallback
_FALLBACK_DPIS = (400, 600, 300)
_DECODE_TIMEOUT = 15


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
        timeout=_DECODE_TIMEOUT,
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
        timeout=_DECODE_TIMEOUT,
    )
    if proc.returncode not in (0, 4):  # 4 = found codes
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def _enhance_page_image(image):
    """Kontrast für schwache QR auf Scan/Archiv-PDF."""
    from PIL import ImageOps
    gray = ImageOps.autocontrast(image.convert("L"))
    return gray.convert("RGB")


def _decode_page_image(image) -> list[str]:
    """Barcodes von gerendeter Seite (ohne erneutes PDF-Rendern)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for variant in (image.convert("RGB"), _enhance_page_image(image)):
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


def _markers_from_pages(
    images: list,
    *,
    regex: str,
) -> tuple[list[tuple[str, int]], list[dict]]:
    pattern = re.compile(regex)
    markers: list[tuple[str, int]] = []
    qr_debug: list[dict] = []
    for page_num, image in enumerate(images, start=1):
        page_hits: list[str] = []
        for raw in _decode_page_image(image):
            hit = _match_barcode(raw, pattern)
            qr_debug.append({"page": page_num, "raw": raw, "matched": bool(hit)})
            if hit and hit not in page_hits:
                page_hits.append(hit)
        for hit in page_hits:
            if markers and markers[-1] == (hit, page_num):
                continue
            log.info("QR Seite %d: %s", page_num, hit)
            markers.append((hit, page_num))
    return markers, qr_debug


def _finalize_markers(
    markers: list[tuple[str, int]],
) -> list[tuple[str, int]]:
    if not markers:
        return markers
    if markers[0][1] > 1:
        log.info("Erster QR auf S.%d — Prefix Kein_Barcode S.1", markers[0][1])
        markers.insert(0, ("Kein_Barcode", 1))
    return markers


def scan_page_qrs(
    pdf_path: str,
    page_num: int,
    *,
    dpi: int = DEFAULT_DPI,
    dpis: tuple[int, ...] | None = None,
) -> list[str]:
    """Alle Barcode-Texte auf Seite page_num (1-basiert) — Legacy-Einzelaufruf."""
    try:
        from pdf2image import convert_from_path
    except ImportError:
        raise RuntimeError("pdf2image fehlt — pip install pdf2image") from None

    tried = dpis or _FALLBACK_DPIS if dpi == DEFAULT_DPI else (dpi,)
    for try_dpi in tried:
        images = convert_from_path(
            pdf_path, dpi=try_dpi, first_page=page_num, last_page=page_num,
        )
        if images:
            found = _decode_page_image(images[0])
            if found:
                return found
    return []


def scan_page_qr(pdf_path: str, page_num: int, *, dpi: int = DEFAULT_DPI) -> str | None:
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
    Rendert PDF einmal pro DPI (nicht pro Seite) — deutlich schneller.
    """
    try:
        from pdf2image import convert_from_path
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("pdf2image/pypdf fehlt") from e

    total = len(PdfReader(pdf_path).pages)
    dpis = _FALLBACK_DPIS if dpi == DEFAULT_DPI else (dpi,)
    best_markers: list[tuple[str, int]] = []
    best_debug: list[dict] = []

    for try_dpi in dpis:
        try:
            images = convert_from_path(pdf_path, dpi=try_dpi)
        except Exception as e:
            log.warning("PDF render dpi=%s fehlgeschlagen: %s", try_dpi, e)
            continue
        if len(images) != total:
            log.warning("PDF render dpi=%s: %d Bilder, erwartet %d", try_dpi, len(images), total)
        markers, qr_debug = _markers_from_pages(images, regex=regex)
        markers = _finalize_markers(markers)
        if _marker_score(markers) > _marker_score(best_markers):
            best_markers, best_debug = markers, qr_debug
        if _marker_score(markers) >= 2:
            break  # genug Marker — höhere DPI spart Zeit

    return best_markers, total, best_debug


def has_real_qr_splits(markers: list[tuple[str, int]]) -> bool:
    return any(name != "Kein_Barcode" for name, _ in markers)


def split_pdf_at_markers(
    pdf_path: str,
    output_dir: str | Path,
    markers: list[tuple[str, int]],
    total: int,
    *,
    source_basename: str | None = None,
) -> list[dict]:
    """PDF an vorberechneten Markern splitten (ohne erneuten QR-Scan)."""
    from pypdf import PdfReader, PdfWriter

    if not has_real_qr_splits(markers):
        return []

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    f_stem = _basename_stem(source_basename or Path(pdf_path).name)
    reader = PdfReader(str(pdf_path))
    results: list[dict] = []

    for i, (barcode, from_page) in enumerate(markers):
        to_page = markers[i + 1][1] - 1 if i + 1 < len(markers) else total
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


def split_pdf_by_qr(
    pdf_path: str,
    output_dir: str | Path,
    *,
    regex: str = DEFAULT_QR_REGEX,
    dpi: int = DEFAULT_DPI,
    source_basename: str | None = None,
) -> list[dict]:
    markers, total, _ = find_split_markers(pdf_path, regex=regex, dpi=dpi)
    return split_pdf_at_markers(
        pdf_path, output_dir, markers, total, source_basename=source_basename,
    )


def _marker_score(markers: list[tuple[str, int]]) -> int:
    return sum(1 for name, _ in markers if name != "Kein_Barcode")


def _scan_pdf_bytes(label: str, pdf_bytes: bytes, *, regex: str, dpi: int) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_bytes)
        tmp = tf.name
    try:
        markers, total, qr_debug = find_split_markers(tmp, regex=regex, dpi=dpi)
        score = _marker_score(markers)
        seen = sum(1 for x in qr_debug if x.get("matched"))
        log.info(
            "Legacy-Split Scan %s: %d Seiten, %d Marker, %d QR-Treffer",
            label, total, score, seen,
        )
        return {
            "rank": (score, seen),
            "label": label,
            "pdf_bytes": pdf_bytes,
            "markers": markers,
            "total": total,
            "qr_debug": qr_debug,
        }
    finally:
        Path(tmp).unlink(missing_ok=True)


def resolve_best_pdf_for_split(
    pdf_variants: list[tuple[str, bytes]],
    *,
    regex: str = DEFAULT_QR_REGEX,
    dpi: int = DEFAULT_DPI,
) -> tuple[str, bytes, list[tuple[str, int]], int, list[dict]] | None:
    """Original + Archiv parallel scannen — Variante mit meisten Markern gewinnt."""
    if not pdf_variants:
        return None
    best: dict | None = None
    workers = min(2, len(pdf_variants))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_scan_pdf_bytes, label, data, regex=regex, dpi=dpi): label
            for label, data in pdf_variants if data
        }
        for fut in as_completed(futures):
            try:
                candidate = fut.result()
            except Exception as e:
                log.warning("Legacy-Split Scan %s fehlgeschlagen: %s", futures[fut], e)
                continue
            if best is None or candidate["rank"] > best["rank"]:
                best = candidate
    if best is None:
        return None
    return (
        best["label"],
        best["pdf_bytes"],
        best["markers"],
        best["total"],
        best["qr_debug"],
    )


def split_paperless_document(
    pdf_bytes: bytes,
    output_dir: str | Path,
    *,
    original_filename: str = "document.pdf",
    regex: str = DEFAULT_QR_REGEX,
    dpi: int = DEFAULT_DPI,
    markers: list[tuple[str, int]] | None = None,
    total: int | None = None,
) -> list[dict]:
    """PDF-Bytes splitten — optional mit vorberechneten Markern (kein Doppel-Scan)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_bytes)
        tmp_pdf = tf.name
    try:
        if markers is not None and total is not None:
            return split_pdf_at_markers(
                tmp_pdf, output_dir, markers, total,
                source_basename=original_filename,
            )
        return split_pdf_by_qr(
            tmp_pdf, output_dir,
            regex=regex, dpi=dpi, source_basename=original_filename,
        )
    finally:
        Path(tmp_pdf).unlink(missing_ok=True)
