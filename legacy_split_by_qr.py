"""
Legacy QR-Split — Port von tsa_barcode_split_function.sh (zbar + Regex).

Pro Seite QR lesen; Treffer ^[0-9]{6}_…$ = Start eines Teildokuments.
Ausgabe: ocrscan_{barcode}_{basename}_p{von}_bis_p{bis}.pdf
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

log = logging.getLogger("legacy_split_by_qr")

DEFAULT_QR_REGEX = r"^[0-9]{6}_[^\s]+$"


_MAX_USER_REGEX_LEN = 200
# Nested quantifiers / repeated wildcards — häufige ReDoS-Muster
_UNSAFE_REGEX_RE = re.compile(
    r"\([^)]*[+*][^)]*\)[+*{?]|([+*?]){2,}|\(\?[^)]*[+*]{2,}"
)


class UnsafeRegexError(ValueError):
    """Regex abgelehnt (Syntax, Länge oder ReDoS-Risiko)."""


def validate_user_regex(regex: str, *, context: str = "regex") -> str:
    """User-/API-Regex prüfen bevor compile/finditer (Legacy-Split, Extraktions-Muster)."""
    s = (regex or "").strip()
    if not s:
        raise UnsafeRegexError(f"{context}: leer")
    if len(s) > _MAX_USER_REGEX_LEN:
        raise UnsafeRegexError(f"{context}: max {_MAX_USER_REGEX_LEN} Zeichen")
    if _UNSAFE_REGEX_RE.search(s):
        raise UnsafeRegexError(f"{context}: riskante Quantifier-Konstrukte")
    try:
        re.compile(s)
    except re.error as e:
        raise UnsafeRegexError(f"{context}: ungültig ({e})") from e
    return s


def normalize_legacy_qr_regex(regex: str | None) -> str:
    """
    .env ohne Quotes frisst \\s → «[^s]» und $ → «\\$» — dann 0 Treffer, Scan ewig.
  In /opt/paperless/.env: LEGACY_SPLIT_QR_REGEX='^[0-9]{6}_[^\\s]+$'
    """
    s = (regex or "").strip()
    if not s:
        return DEFAULT_QR_REGEX
    if "[^s]" in s and "[^\s]" not in s and r"\s" not in s:
        log.warning(
            "LEGACY_SPLIT_QR_REGEX fehlerhaft (%r) — \\s in .env mit Quotes setzen, nutze Default",
            s,
        )
        return DEFAULT_QR_REGEX
    if s.endswith(r"\$"):
        s = s[:-2] + "$"
    try:
        validate_user_regex(s, context="LEGACY_SPLIT_QR_REGEX")
    except UnsafeRegexError as e:
        log.warning("%s — nutze Default", e)
        return DEFAULT_QR_REGEX
    return s


DEFAULT_DPI = 150  # Legacy-Trennseiten: QR reicht ab ~150 dpi (Smartphone-Niveau)
_FALLBACK_DPIS = (150, 200, 300, 400, 600)
_DECODE_TIMEOUT = 15
# Original tsa_barcode_split_function.sh: Ghostscript 600 dpi — Poppler rendert manche NAS-Scans ohne QR
_RENDER_BACKENDS = ("ghostscript", "poppler")


def _barcode_text(raw: bytes) -> str | None:
    for enc in ("utf-8", "latin-1"):
        try:
            text = raw.decode(enc, errors="replace").strip()
        except Exception:
            continue
        if text:
            return text
    return None


def _decode_barcodes_from_pil(image) -> list[str]:
    """Inline pyzbar — schnell; bei Segfault siehe Subprocess-Fallback."""
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
    except ImportError:
        return []
    codes: list[str] = []
    seen: set[str] = set()
    for barcode in pyzbar_decode(image):
        text = _barcode_text(barcode.data)
        if text and text not in seen:
            seen.add(text)
            codes.append(text)
    return codes


def _decode_barcodes_from_image_path(img_path: str) -> list[str]:
    """Alle lesbaren Barcode-Texte von PNG (Subprocess-pyzbar, dann zbarimg)."""
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
            log.debug("pyzbar subprocess: JSON ungültig: %r", proc.stdout[:120])
    else:
        err = (proc.stderr or proc.stdout or "").strip()
        log.debug(
            "pyzbar subprocess exit %s: %s",
            proc.returncode,
            err[:240] or "(keine Ausgabe)",
        )
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
    from PIL import ImageFilter, ImageOps
    gray = ImageOps.autocontrast(image.convert("L"))
    sharp = gray.filter(ImageFilter.SHARPEN)
    return sharp.convert("RGB")


def _page_scan_variants(image):
    """Vollseite + Legacy-Trennseite oben rechts (QR neben «010401_Lohn_…»)."""
    from PIL import Image
    rgb = image.convert("RGB")
    w, h = rgb.size
    yield rgb
    yield _enhance_page_image(rgb)
    # Trennseiten: QR typisch oben rechts (~15 % Seitenbreite)
    for x_frac, y_frac, w_frac, h_frac in (
        (0.55, 0.0, 0.45, 0.28),
        (0.45, 0.0, 0.55, 0.35),
        (0.60, 0.0, 0.40, 0.22),
        (0.0, 0.0, 1.0, 0.20),
    ):
        box = (int(w * x_frac), int(h * y_frac), int(w * (x_frac + w_frac)), int(h * (y_frac + h_frac)))
        crop = rgb.crop(box)
        yield crop
        yield _enhance_page_image(crop)
        if max(crop.size) < 2400:
            up = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
            yield up
            yield _enhance_page_image(up)


def _collect_barcodes(raw_list: list[str], seen: list[str], seen_set: set[str]) -> bool:
    added = False
    for raw in raw_list:
        if raw not in seen_set:
            seen_set.add(raw)
            seen.append(raw)
            added = True
    return added


def _decode_page_image(image) -> list[str]:
    """Barcodes von gerendeter Seite (ohne erneutes PDF-Rendern)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    # pyzbar/libzbar nur im Hauptthread — sonst Deadlock (UI-Executor-Thread)
    use_inline = threading.current_thread() is threading.main_thread()
    for variant in _page_scan_variants(image):
        if use_inline:
            if _collect_barcodes(_decode_barcodes_from_pil(variant), seen, seen_set):
                return seen
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tmp = tf.name
        try:
            variant.save(tmp, format="PNG")
            if _collect_barcodes(_decode_barcodes_from_image_path(tmp), seen, seen_set):
                return seen
        finally:
            Path(tmp).unlink(missing_ok=True)
    return seen


def _convert_pdf_poppler(
    pdf_path: str,
    *,
    dpi: int,
    first_page: int | None = None,
    last_page: int | None = None,
) -> list:
    from pdf2image import convert_from_path

    kwargs: dict = {"dpi": dpi, "use_pdftocairo": True}
    if first_page is not None:
        kwargs["first_page"] = first_page
    if last_page is not None:
        kwargs["last_page"] = last_page
    return convert_from_path(pdf_path, **kwargs)


def _convert_pdf_ghostscript(
    pdf_path: str,
    *,
    dpi: int,
    first_page: int | None = None,
    last_page: int | None = None,
) -> list:
    """Wie tsa_barcode_split_function.sh — zuverlässiger bei älteren NAS-Scans."""
    if not shutil.which("gs"):
        log.debug("ghostscript (gs) nicht installiert")
        return []
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="legacy_qr_gs_") as td:
        pattern = str(Path(td) / "page-%d.png")
        cmd = [
            "gs",
            "-dNOPAUSE",
            "-dBATCH",
            "-dSAFER",
            "-sDEVICE=png16m",
            f"-r{dpi}",
            f"-sOutputFile={pattern}",
        ]
        if first_page is not None:
            cmd.extend([f"-dFirstPage={first_page}", f"-dLastPage={last_page or first_page}"])
        cmd.append(str(pdf_path))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            log.warning("Ghostscript dpi=%s fehlgeschlagen: %s", dpi, err[:240])
            return []
        paths = sorted(Path(td).glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
        return [Image.open(p).copy() for p in paths]


def convert_pdf_pages(
    pdf_path: str,
    *,
    dpi: int,
    backend: str = "poppler",
    first_page: int | None = None,
    last_page: int | None = None,
) -> list:
    if backend == "ghostscript":
        return _convert_pdf_ghostscript(
            pdf_path, dpi=dpi, first_page=first_page, last_page=last_page,
        )
    return _convert_pdf_poppler(
        pdf_path, dpi=dpi, first_page=first_page, last_page=last_page,
    )


def dump_rendered_page(
    pdf_path: str,
    page_num: int,
    out_path: str | Path,
    *,
    dpi: int = 150,
    backend: str = "ghostscript",
) -> Path:
    """Gerenderte Seite als PNG speichern (Debug: ist der QR im Raster?)."""
    images = convert_pdf_pages(
        pdf_path, dpi=dpi, backend=backend,
        first_page=page_num, last_page=page_num,
    )
    if not images:
        raise RuntimeError(f"Seite {page_num} nicht gerendert (backend={backend}, dpi={dpi})")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(out, format="PNG")
    return out


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
    tried = dpis or _FALLBACK_DPIS if dpi == DEFAULT_DPI else (dpi,)
    for backend in _RENDER_BACKENDS:
        for try_dpi in tried:
            images = convert_pdf_pages(
                pdf_path, dpi=try_dpi, backend=backend,
                first_page=page_num, last_page=page_num,
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
    backends: tuple[str, ...] | None = None,
    dpis: tuple[int, ...] | None = None,
) -> tuple[list[tuple[str, int]], int, list[dict], dict]:
    """
    [(barcode, start_page), …] — 1-basierte Startseiten.
    Rendert PDF einmal pro DPI/Backend (Poppler, dann Ghostscript wie Bash-Original).
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("pypdf fehlt") from e

    total = len(PdfReader(pdf_path).pages)
    use_backends = backends or _RENDER_BACKENDS
    use_dpis = dpis or (_FALLBACK_DPIS if dpi == DEFAULT_DPI else (dpi,))
    best_markers: list[tuple[str, int]] = []
    best_debug: list[dict] = []
    best_meta: dict = {}

    for backend in use_backends:
        for try_dpi in use_dpis:
            try:
                images = convert_pdf_pages(pdf_path, dpi=try_dpi, backend=backend)
            except Exception as e:
                log.warning("PDF render %s dpi=%s fehlgeschlagen: %s", backend, try_dpi, e)
                continue
            if len(images) != total:
                log.warning(
                    "PDF render %s dpi=%s: %d Bilder, erwartet %d",
                    backend, try_dpi, len(images), total,
                )
            markers, qr_debug = _markers_from_pages(images, regex=regex)
            markers = _finalize_markers(markers)
            score = _marker_score(markers)
            if score > _marker_score(best_markers):
                best_markers, best_debug = markers, qr_debug
                best_meta = {"backend": backend, "dpi": try_dpi}
                if score:
                    log.info(
                        "Legacy QR: %d Marker via %s @ %d dpi",
                        score, backend, try_dpi,
                    )
            if score >= 2:
                return best_markers, total, best_debug, best_meta
        if _marker_score(best_markers) >= 1:
            break

    return best_markers, total, best_debug, best_meta


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
    markers, total, _, _ = find_split_markers(pdf_path, regex=regex, dpi=dpi)
    return split_pdf_at_markers(
        pdf_path, output_dir, markers, total, source_basename=source_basename,
    )


def _marker_score(markers: list[tuple[str, int]]) -> int:
    return sum(1 for name, _ in markers if name != "Kein_Barcode")


def _scan_pdf_file_isolated(
    pdf_path: str,
    label: str,
    *,
    regex: str,
    dpi: int,
    quick: bool,
) -> dict:
    """Separater Python-Prozess — identisch mit legacy_qr_split_test.py / CLI."""
    worker = Path(__file__).resolve().parent / "legacy_qr_scan_worker.py"
    if not worker.is_file():
        raise RuntimeError(f"legacy_qr_scan_worker.py fehlt neben {__file__}")
    python = os.environ.get("LEGACY_QR_PYTHON", sys.executable)
    regex = normalize_legacy_qr_regex(regex)
    cmd = [python, str(worker), pdf_path]
    if quick:
        cmd.append("--quick")
    env = os.environ.copy()
    env["LEGACY_SPLIT_QR_REGEX"] = regex
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(worker.parent),
        env=env,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err[:500] or f"scan worker exit {proc.returncode}")
    data = json.loads(proc.stdout.strip() or "{}")
    markers = [tuple(m) for m in data.get("markers", [])]
    total = int(data.get("total", 0))
    qr_debug = data.get("qr_debug") or []
    scan_meta = data.get("scan_meta") or {}
    pdf_bytes = Path(pdf_path).read_bytes()
    score = _marker_score(markers)
    log.info(
        "Legacy-Split Scan %s (subprocess): %d Seiten, %d Marker",
        label, total, score,
    )
    return {
        "label": label,
        "pdf_bytes": pdf_bytes,
        "markers": markers,
        "total": total,
        "qr_debug": qr_debug,
        "scan_meta": {**scan_meta, "path": pdf_path, "isolated": True},
        "rank": (score, sum(1 for x in qr_debug if x.get("matched"))),
    }


def scan_pdf_file(
    pdf_path: str,
    label: str = "file",
    *,
    regex: str = DEFAULT_QR_REGEX,
    dpi: int = DEFAULT_DPI,
    quick: bool = False,
    isolated: bool | None = None,
) -> dict:
    """PDF direkt vom Dateisystem scannen (wie legacy_qr_split_test.py)."""
    if isolated is None:
        isolated = threading.current_thread() is not threading.main_thread()
    if isolated:
        return _scan_pdf_file_isolated(pdf_path, label, regex=regex, dpi=dpi, quick=quick)

    backends = ("ghostscript",) if quick else _RENDER_BACKENDS
    dpis = (150, 300) if quick else None
    markers, total, qr_debug, scan_meta = find_split_markers(
        pdf_path, regex=regex, dpi=dpi, backends=backends, dpis=dpis,
    )
    pdf_bytes = Path(pdf_path).read_bytes()
    score = _marker_score(markers)
    return {
        "label": label,
        "pdf_bytes": pdf_bytes,
        "markers": markers,
        "total": total,
        "qr_debug": qr_debug,
        "scan_meta": {**(scan_meta or {}), "path": pdf_path},
        "rank": (score, sum(1 for x in qr_debug if x.get("matched"))),
    }


def _scan_pdf_bytes_quick(label: str, pdf_bytes: bytes, *, regex: str, dpi: int) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_bytes)
        tmp = tf.name
    try:
        return scan_pdf_file(tmp, label, regex=regex, dpi=dpi, quick=True)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _scan_pdf_bytes(label: str, pdf_bytes: bytes, *, regex: str, dpi: int) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_bytes)
        tmp = tf.name
    try:
        markers, total, qr_debug, scan_meta = find_split_markers(tmp, regex=regex, dpi=dpi)
        score = _marker_score(markers)
        seen = sum(1 for x in qr_debug if x.get("matched"))
        log.info(
            "Legacy-Split Scan %s: %d Seiten, %d Marker, %d QR-Treffer (%s @ %sdpi)",
            label, total, score, seen,
            scan_meta.get("backend", "?"), scan_meta.get("dpi", "?"),
        )
        return {
            "rank": (score, seen),
            "label": label,
            "pdf_bytes": pdf_bytes,
            "markers": markers,
            "total": total,
            "qr_debug": qr_debug,
            "scan_meta": scan_meta,
        }
    finally:
        Path(tmp).unlink(missing_ok=True)


def resolve_best_pdf_for_split(
    pdf_variants: list[tuple[str, bytes]],
    *,
    regex: str = DEFAULT_QR_REGEX,
    dpi: int = DEFAULT_DPI,
    min_score_skip_rest: int = 1,
) -> tuple[str, bytes, list[tuple[str, int]], int, list[dict], dict] | None:
    """
    PDF-Varianten scannen — Original zuerst.
    Archiv wird übersprungen, sobald Original genug QR-Marker hat (spart Minuten).
    """
    if not pdf_variants:
        return None
    by_label = {label: data for label, data in pdf_variants if data}
    order: list[tuple[str, bytes]] = []
    if "original" in by_label:
        order.append(("original", by_label["original"]))
    if "archiv" in by_label:
        order.append(("archiv", by_label["archiv"]))
    for label, data in pdf_variants:
        if label not in ("original", "archiv") and data:
            order.append((label, data))

    best: dict | None = None
    for label, data in order:
        try:
            candidate = _scan_pdf_bytes(label, data, regex=regex, dpi=dpi)
        except Exception as e:
            log.warning("Legacy-Split Scan %s fehlgeschlagen: %s", label, e)
            continue
        if best is None or candidate["rank"] > best["rank"]:
            best = candidate
        if label == "original" and candidate["rank"][0] >= min_score_skip_rest:
            log.info(
                "Legacy-Split: Original %d Marker — weitere Varianten übersprungen",
                candidate["rank"][0],
            )
            break
    if best is None:
        return None
    return (
        best["label"],
        best["pdf_bytes"],
        best["markers"],
        best["total"],
        best["qr_debug"],
        best.get("scan_meta") or {},
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
