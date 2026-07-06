#!/usr/bin/env python3
"""
CLI: Handschrift / Schulbericht — Vision-LLM (qwen2.5vl) testen und tunen.

Nur Kommandozeile — kein UI. Vergleicht Prompt-Varianten mit detaillierten Logs.

CT121 Beispiele (liest /opt/paperless-scripts/.env und /opt/paperless/.env):
  /opt/paperless-scripts/venv/bin/python3 /opt/paperless-scripts/test_handwriting_vision.py \\
    --doc-id 3577 --mode all -v -o /tmp/htr-3577.json

  /opt/paperless-scripts/venv/bin/python3 test_handwriting_vision.py \\
    /pfad/zum/scan.pdf --mode schulbericht --page 1 --num-predict 1024

Modi:
  pipeline       — HTR aller Seiten → Text-Extract (empfohlen, wie post_consume v12.65)
  transcribe     — nur Stufe 1: zeilengetreue HTR
  extract        — nur Stufe 2: JSON aus --transcript-file
  schulbericht-e2e — E2E-Vision (Debug/Vergleich, alias: schulbericht)
  baseline       — Pipeline-Standard-Prompt Seite 1
  all            — pipeline + schulbericht-e2e + baseline
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schulbericht_vision import (  # noqa: E402
    EXTRACT_SYSTEM,
    HTR_NUM_PREDICT,
    HTR_VISION_SYSTEM,
    SCHULBERICHT_DPI,
    SCHULBERICHT_NUM_PREDICT,
    SCHULBERICHT_VISION_SYSTEM,
    build_extract_from_transcript_prompt,
    build_htr_transcribe_prompt,
    build_schulbericht_vision_prompt,
    estimate_htr_confidence,
    merge_htr_transcribe_pages,
    merge_schulbericht_pages,
    normalize_extracted_schulbericht,
    pdf_page_count,
    schulbericht_to_vision_meta,
)

log = logging.getLogger("test_handwriting_vision")


def _load_env_files() -> None:
    """Token und Pfade aus üblichen Server-.env-Dateien (wie backfill_dok_id.py)."""
    candidates = [
        Path("/opt/paperless-scripts/.env"),
        Path("/opt/paperless/.env"),
        ROOT / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        log.debug("Lade Env: %s", path)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_env_files()

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL_VISION = os.environ.get("OLLAMA_MODEL_VISION", "qwen2.5vl:7b")
MODEL_LLM = os.environ.get("OLLAMA_MODEL_LLM", "llama3.3:70b")
MODEL_EXTRACT = os.environ.get("SCHULBERICHT_EXTRACT_MODEL", MODEL_LLM)
VISION_TIMEOUT = int(os.environ.get("VISION_TIMEOUT", "120"))
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "300"))
MEDIA_ROOT = Path(os.environ.get("PAPERLESS_MEDIA_ROOT", "/mnt/paperless-media"))
PAPERLESS_URL = (
    os.environ.get("PAPERLESS_INTERNAL_URL")
    or os.environ.get("PAPERLESS_URL", "http://localhost:8000")
).rstrip("/")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN") or os.environ.get("PAPERLESS_API_TOKEN", "")

VISION_SYSTEM_JSON = (
    "Du bist ein JSON-Extraktor für Schweizer Dokumente. "
    "Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt. "
    "Kein Text davor oder danach. Kein Markdown."
)
VISION_SYSTEM_HTR = HTR_VISION_SYSTEM

MODES = ("pipeline", "transcribe", "extract", "schulbericht-e2e", "schulbericht", "baseline", "all")
DEFAULT_NUM_PREDICT = {
    "baseline": 300,
    "pipeline": HTR_NUM_PREDICT,
    "transcribe": HTR_NUM_PREDICT,
    "extract": 1024,
    "schulbericht-e2e": SCHULBERICHT_NUM_PREDICT,
    "schulbericht": SCHULBERICHT_NUM_PREDICT,
}


@dataclass
class RunResult:
    mode: str
    page: int
    model: str
    num_predict: int
    elapsed_s: float
    prompt_chars: int
    image_bytes: int
    ocr_chars: int
    raw_response: str
    parsed: Any
    ollama_stats: dict = field(default_factory=dict)
    error: Optional[str] = None


def _find_json_object(text: str) -> str:
    depth = 0
    start: Optional[int] = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : i + 1]
    return ""


def extract_json_from_response(raw: str) -> dict | list | None:
    raw = raw.strip()
    raw = re.sub(
        r"<think>.*?</think>", "", raw, flags=re.DOTALL
    ).strip()
    for candidate in (raw,):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    md = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL)
    if md:
        try:
            return json.loads(md.group(1))
        except json.JSONDecodeError:
            pass
    brace = _find_json_object(raw)
    if brace:
        try:
            return json.loads(brace)
        except json.JSONDecodeError:
            pass
    return None


@dataclass
class ResolvedInput:
    path: Path
    temp_pdf: Optional[Path] = None


def _paperless_headers() -> dict:
    return {
        "Authorization": f"Token {PAPERLESS_TOKEN}",
        "Accept": "application/json",
    }


def _paperless_get(endpoint: str) -> dict:
    url = f"{PAPERLESS_URL}/api/{endpoint.lstrip('/')}"
    r = requests.get(url, headers=_paperless_headers(), timeout=30)
    r.raise_for_status()
    return r.json() if r.text.strip() else {}


def _find_pdf_by_padded_id(doc_id: str | int) -> Optional[Path]:
    padded = str(doc_id).zfill(7)
    for sub in ("originals", "archive"):
        p = MEDIA_ROOT / "documents" / sub / f"{padded}.pdf"
        if p.is_file():
            return p
    return None


def _pdf_path_from_paperless_meta(document_id: int) -> Optional[Path]:
    """Dateisystem: Speicherpfad + Dateiname aus Paperless-API (wie post_consume)."""
    try:
        doc = _paperless_get(f"documents/{document_id}/")
    except Exception as e:
        log.warning("Dok #%s: API-Metadaten für PDF-Pfad: %s", document_id, e)
        return None

    media = MEDIA_ROOT / "documents" / "originals"
    fn = (doc.get("original_file_name") or "").strip()
    if not fn:
        title = (doc.get("title") or "").strip()
        if title:
            fn = title if title.lower().endswith(".pdf") else f"{title}.pdf"
    archive_fn = (doc.get("archive_filename") or "").strip()
    sp_subpath = ""
    sp_id = doc.get("storage_path")
    if sp_id:
        try:
            sp = _paperless_get(f"storage_paths/{sp_id}/")
            sp_subpath = (sp.get("path") or sp.get("name") or "").strip().strip("/\\")
        except Exception as e:
            log.warning("Dok #%s: storage_path #%s: %s", document_id, sp_id, e)

    candidates: list[Path] = []
    padded = f"{document_id:07d}.pdf"
    for name in (fn, archive_fn):
        if not name:
            continue
        if sp_subpath:
            candidates.append(media / sp_subpath / name)
        candidates.append(media / name)
    if sp_subpath:
        candidates.append(media / sp_subpath / padded)
    candidates.append(media / padded)
    candidates.append(media / f"{document_id}.pdf")

    seen: set[str] = set()
    for c in candidates:
        key = str(c)
        if key in seen:
            continue
        seen.add(key)
        if c.is_file():
            log.info("PDF für Dok #%s auf Dateisystem: %s", document_id, c)
            return c

    if fn:
        base = os.path.basename(fn)
        try:
            for p in media.rglob(base):
                if p.is_file():
                    log.info("PDF für Dok #%s via Suche (%s): %s", document_id, base, p)
                    return p
        except OSError as e:
            log.warning("Dok #%s: rglob(%s): %s", document_id, base, e)
    return None


def _download_pdf_via_api(document_id: int) -> Optional[Path]:
    if not PAPERLESS_TOKEN:
        log.warning("Dok #%s: kein PAPERLESS_TOKEN für API-Download", document_id)
        return None
    try:
        r = requests.get(
            f"{PAPERLESS_URL}/api/documents/{document_id}/download/",
            headers={"Authorization": f"Token {PAPERLESS_TOKEN}"},
            timeout=120,
        )
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(r.content)
            tmp_path = Path(tmp.name)
        log.info("PDF für Dok #%s via API geladen (%d bytes) → %s", document_id, len(r.content), tmp_path)
        return tmp_path
    except Exception as e:
        log.warning("PDF API-Download für Dok #%s fehlgeschlagen: %s", document_id, e)
        return None


def resolve_document_pdf(document_id: int) -> ResolvedInput:
    """PDF für Vision: API-Metadaten → gepaddete ID → API-Download."""
    path = _pdf_path_from_paperless_meta(document_id)
    if path:
        return ResolvedInput(path=path)
    path = _find_pdf_by_padded_id(document_id)
    if path:
        log.info("PDF für Dok #%s (gepaddet): %s", document_id, path)
        return ResolvedInput(path=path)
    tmp = _download_pdf_via_api(document_id)
    if tmp:
        return ResolvedInput(path=tmp, temp_pdf=tmp)
    raise FileNotFoundError(
        f"PDF für Dok #{document_id} nicht gefunden "
        f"(MEDIA_ROOT={MEDIA_ROOT}, API={PAPERLESS_URL})"
    )


def pdftotext(pdf_path: Path, page: Optional[int] = None) -> str:
    cmd = ["pdftotext", "-layout"]
    if page is not None:
        cmd.extend(["-f", str(page), "-l", str(page)])
    cmd.extend([str(pdf_path), "-"])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
        return (r.stdout or "").strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("pdftotext fehlgeschlagen: %s", e)
        return ""


def file_to_base64_image(path: Path, page: int = 1, dpi: int = 150) -> tuple[str, int]:
    """PDF-Seite oder Bilddatei → JPEG base64. Returns (b64, byte_size)."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                [
                    "gs", "-dNOPAUSE", "-dBATCH", "-sDEVICE=jpeg",
                    f"-dFirstPage={page}", f"-dLastPage={page}", f"-r{dpi}",
                    f"-sOutputFile={tmp_path}", str(path),
                ],
                capture_output=True,
                check=True,
                timeout=120,
            )
            data = Path(tmp_path).read_bytes()
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    elif suffix in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"):
        if suffix in (".png", ".webp", ".tif", ".tiff"):
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                subprocess.run(
                    [
                        "gs", "-dNOPAUSE", "-dBATCH", "-sDEVICE=jpeg",
                        f"-r{dpi}", f"-sOutputFile={tmp_path}", str(path),
                    ],
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                data = Path(tmp_path).read_bytes()
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        else:
            data = path.read_bytes()
    else:
        raise ValueError(f"Nicht unterstützt: {suffix} (PDF oder Bild erwartet)")
    return base64.b64encode(data).decode(), len(data)


def build_baseline_prompt(ocr_text: str) -> str:
    """Gleicher Fokus wie post_consume.vision_analyze (ohne Haushalt-Kontext)."""
    ocr_snip = (ocr_text or "")[:1500]
    return (
        "Extrahiere aus diesem Schweizer Dokument folgende Felder als JSON.\n"
        "Achte besonders auf handschriftliche Notizen oben rechts am Rand "
        "(meist ein Bezahlt-Vermerk wie 'bez. 6.2.26' oder 'bez 26.3.26' oder 'EZ 26.3.26').\n"
        '{"absender": "Firmenname oder Behörde nicht Empfänger", '
        '"empfaenger": "Name des Empfängers", '
        '"datum": "YYYY-MM-DD oder null", '
        '"betrag": "Zahlungsbetrag CHF XX.XX oder null", '
        '"rechnungsnummer": "Rechnungs-/Fakturanummer oder null", '
        '"kennzeichen": "Fahrzeugkennzeichen oder null", '
        '"dokumenttyp_visuell": "z.B. Rechnung/Schulbericht/Brief", '
        '"layout": "Beschreibung des Layouts", '
        '"logo_vorhanden": true, '
        '"tabellen_vorhanden": true, '
        '"qr_einzahlungsschein": true, '
        '"sprache": "de/fr/it/en", '
        '"handschrift": "handschriftliche Notiz exakt abschreiben — null wenn keine", '
        '"besonderheiten": "wichtige Zusatzinfos oder null"}\n\n'
        f"OCR-Text (Zusatzinfo, oft leer bei Handschrift):\n{ocr_snip or '(leer)'}"
    )


def build_transcribe_prompt(ocr_text: str, page: int = 1, page_total: int = 1) -> str:
    return build_htr_transcribe_prompt(page, page_total)


def merge_transcribe_pages(pages: list[dict]) -> dict:
    merged = merge_htr_transcribe_pages(pages)
    merged["htr_confidence"] = estimate_htr_confidence(merged)
    return merged


def prompt_for_mode(
    mode: str, ocr_text: str, page: int = 1, page_total: int = 1,
) -> tuple[str, str]:
    m = "schulbericht-e2e" if mode == "schulbericht" else mode
    if m == "baseline":
        return build_baseline_prompt(ocr_text), VISION_SYSTEM_JSON
    if m == "schulbericht-e2e":
        return build_schulbericht_vision_prompt(ocr_text, page, page_total), SCHULBERICHT_VISION_SYSTEM
    if m in ("transcribe", "pipeline"):
        return build_htr_transcribe_prompt(page, page_total), HTR_VISION_SYSTEM
    raise ValueError(f"Modus ohne Bild-Prompt: {mode}")


def ollama_vision_chat(
    image_b64: str,
    user_prompt: str,
    system: str,
    model: str,
    num_predict: int,
    temperature: float,
) -> tuple[str, dict]:
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": user_prompt,
            "images": [image_b64],
        }],
        "system": system,
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    url = f"{OLLAMA_BASE.rstrip('/')}/api/chat"
    log.debug("POST %s model=%s num_predict=%d prompt_len=%d", url, model, num_predict, len(user_prompt))
    r = requests.post(url, json=payload, timeout=VISION_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    raw = data.get("message", {}).get("content", "")
    stats = {
        k: data.get(k)
        for k in (
            "total_duration", "load_duration", "prompt_eval_count",
            "prompt_eval_duration", "eval_count", "eval_duration",
        )
        if data.get(k) is not None
    }
    return raw, stats


def ollama_text_chat(
    user_prompt: str,
    system: str,
    model: str,
    num_predict: int,
    temperature: float,
) -> tuple[str, dict]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": user_prompt}],
        "system": system,
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    url = f"{OLLAMA_BASE.rstrip('/')}/api/chat"
    r = requests.post(url, json=payload, timeout=LLM_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    raw = data.get("message", {}).get("content", "")
    stats = {k: data.get(k) for k in ("eval_count", "eval_duration") if data.get(k) is not None}
    return raw, stats


def run_extract(transcript: str, model: str, num_predict: int) -> tuple[dict, str, float]:
    prompt = build_extract_from_transcript_prompt(transcript)
    t0 = time.perf_counter()
    raw, _ = ollama_text_chat(prompt, EXTRACT_SYSTEM, model, num_predict, 0.0)
    parsed = extract_json_from_response(raw) or {}
    elapsed = time.perf_counter() - t0
    return parsed if isinstance(parsed, dict) else {}, raw, elapsed


def run_mode(
    mode: str,
    image_b64: str,
    image_bytes: int,
    ocr_text: str,
    page: int,
    page_total: int,
    model: str,
    num_predict: int,
    temperature: float,
) -> RunResult:
    user_prompt, system = prompt_for_mode(mode, ocr_text, page, page_total)
    t0 = time.perf_counter()
    error = None
    raw = ""
    stats: dict = {}
    parsed: Any = None
    try:
        raw, stats = ollama_vision_chat(
            image_b64, user_prompt, system, model, num_predict, temperature,
        )
        parsed = extract_json_from_response(raw)
        if parsed is None and raw.strip():
            parsed = {"_parse_failed": True, "_raw_preview": raw[:500]}
    except Exception as e:
        error = str(e)
        log.exception("Modus %s Seite %d fehlgeschlagen", mode, page)
    elapsed = time.perf_counter() - t0
    return RunResult(
        mode=mode,
        page=page,
        model=model,
        num_predict=num_predict,
        elapsed_s=round(elapsed, 2),
        prompt_chars=len(user_prompt),
        image_bytes=image_bytes,
        ocr_chars=len(ocr_text or ""),
        raw_response=raw,
        parsed=parsed,
        ollama_stats=stats,
        error=error,
    )


def log_result(res: RunResult) -> None:
    sep = "─" * 72
    log.info(sep)
    log.info(
        "MODUS=%s  Seite=%d  Modell=%s  num_predict=%d  Zeit=%.1fs  Bild=%d B  OCR=%d Zeichen",
        res.mode, res.page, res.model, res.num_predict, res.elapsed_s,
        res.image_bytes, res.ocr_chars,
    )
    if res.ollama_stats:
        eval_c = res.ollama_stats.get("eval_count")
        if eval_c is not None:
            log.info("  Ollama eval_count=%s", eval_c)
    if res.error:
        log.error("  FEHLER: %s", res.error)
        return
    if res.parsed is not None:
        log.info("  JSON:\n%s", json.dumps(res.parsed, ensure_ascii=False, indent=2))
    else:
        log.warning("  Kein JSON parsebar")
    if res.raw_response:
        preview = res.raw_response if len(res.raw_response) <= 1200 else res.raw_response[:1200] + "…"
        log.info("  Raw (%d Zeichen):\n%s", len(res.raw_response), preview)


def resolve_page_list(
    input_path: Path,
    page: Optional[int],
    pages: Optional[str],
    mode: str,
) -> list[int]:
    m = "schulbericht-e2e" if mode == "schulbericht" else mode
    if m == "extract":
        return []
    if pages:
        return [int(p.strip()) for p in pages.split(",") if p.strip()]
    if page is not None:
        return [page]
    if input_path.suffix.lower() == ".pdf":
        total = pdf_page_count(str(input_path))
        if m == "baseline":
            return [1]
        return list(range(1, total + 1))
    return [1]


def _mode_effective(mode: str) -> str:
    return "schulbericht-e2e" if mode == "schulbericht" else mode


def resolve_input(path: Optional[str], doc_id: Optional[int]) -> ResolvedInput:
    if path:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Datei nicht gefunden: {p}")
        return ResolvedInput(path=p)
    if doc_id is not None:
        return resolve_document_pdf(doc_id)
    raise ValueError("Pfad oder --doc-id angeben")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Handschrift/Schulbericht — Vision-LLM testen (CLI only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("path", nargs="?", help="PDF oder Bild (PNG/JPG)")
    parser.add_argument("--doc-id", type=int, help="Paperless-Dokument-ID (PDF aus MEDIA_ROOT)")
    parser.add_argument(
        "--mode", choices=MODES, default="all",
        help="Prompt-Variante (default: all)",
    )
    parser.add_argument("--page", type=int, help="Nur diese PDF-Seite (sonst alle Seiten)")
    parser.add_argument("--pages", help="Mehrere Seiten, kommagetrennt z.B. 1,2,3")
    parser.add_argument("--dpi", type=int, default=SCHULBERICHT_DPI, help=f"Render-Auflösung (default: {SCHULBERICHT_DPI})")
    parser.add_argument("--model", default=MODEL_VISION, help=f"Vision-Modell (default: {MODEL_VISION})")
    parser.add_argument("--extract-model", default=MODEL_EXTRACT, help=f"Extract-Modell (default: {MODEL_EXTRACT})")
    parser.add_argument("--num-predict", type=int, help="Token-Limit (default je nach Modus)")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--transcript-file", help="Für --mode extract: Transkript-Datei")
    parser.add_argument("--ocr-file", help="OCR-Text aus Datei statt pdftotext")
    parser.add_argument("--ocr-chars", type=int, default=0, help="OCR auf N Zeichen kürzen (0=unbegrenzt)")
    parser.add_argument("--output", "-o", help="Ergebnisse als JSON-Datei speichern")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG-Logs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        resolved = resolve_input(args.path, args.doc_id)
    except (FileNotFoundError, ValueError) as e:
        log.error("%s", e)
        return 2

    input_path = resolved.path
    if args.mode == "all":
        modes = ["pipeline", "schulbericht-e2e", "baseline"]
    else:
        modes = [args.mode]
    pdf_total = pdf_page_count(str(input_path)) if input_path.suffix.lower() == ".pdf" else 1

    try:
        if args.ocr_file:
            ocr_full = Path(args.ocr_file).read_text(encoding="utf-8", errors="replace").strip()
        elif input_path.suffix.lower() == ".pdf":
            ocr_full = pdftotext(input_path)
        else:
            ocr_full = ""

        if args.ocr_chars and len(ocr_full) > args.ocr_chars:
            ocr_full = ocr_full[: args.ocr_chars]

        log.info(
            "Input=%s  PDF-Seiten=%d  MEDIA_ROOT=%s  Modi=%s  Modell=%s  DPI=%d  Ollama=%s",
            input_path, pdf_total, MEDIA_ROOT, modes, args.model, args.dpi, OLLAMA_BASE,
        )
        if ocr_full:
            preview = ocr_full[:200].replace("\n", " ")
            if len(ocr_full) > 200:
                preview += "…"
            log.info("OCR (%d Zeichen): %s", len(ocr_full), preview)
        else:
            log.info("OCR: (leer — typisch bei Handschrift-Scan)")

        all_results: list[dict] = []

        for mode in modes:
            eff = _mode_effective(mode)

            if eff == "extract":
                transcript = ""
                if args.transcript_file:
                    transcript = Path(args.transcript_file).read_text(encoding="utf-8", errors="replace")
                else:
                    log.error("extract braucht --transcript-file")
                    return 2
                npred = args.num_predict or DEFAULT_NUM_PREDICT["extract"]
                extracted, raw, elapsed = run_extract(transcript, args.extract_model, npred)
                htr_stub = {"volltext": transcript, "handschrift_zeilen": transcript.splitlines()}
                normalized = normalize_extracted_schulbericht(extracted, htr=htr_stub)
                log.info("─" * 72)
                log.info("EXTRACT (%.1fs, confidence=%.2f):", elapsed, normalized.get("confidence") or 0)
                log.info("%s", json.dumps(normalized, ensure_ascii=False, indent=2))
                all_results.append({"mode": "extract", "parsed": normalized, "elapsed_s": round(elapsed, 2)})
                continue

            page_list = resolve_page_list(input_path, args.page, args.pages, eff)
            log.info("Modus %s → Seiten %s", mode, page_list or "(extract only)")
            page_parsed: list[dict] = []

            for page in page_list:
                try:
                    image_b64, image_bytes = file_to_base64_image(
                        input_path, page=page, dpi=args.dpi,
                    )
                except Exception as e:
                    log.error("Bild-Rendering Seite %d fehlgeschlagen: %s", page, e)
                    return 1
                log.info("Seite %d/%d gerendert: %d Bytes JPEG", page, pdf_total, image_bytes)

                npred = args.num_predict if args.num_predict is not None else DEFAULT_NUM_PREDICT.get(
                    eff, HTR_NUM_PREDICT,
                )
                run_m = "transcribe" if eff == "pipeline" else eff
                res = run_mode(
                    mode=run_m,
                    image_b64=image_b64,
                    image_bytes=image_bytes,
                    ocr_text=ocr_full,
                    page=page,
                    page_total=pdf_total,
                    model=args.model,
                    num_predict=npred,
                    temperature=args.temperature,
                )
                log_result(res)
                row = asdict(res)
                row["mode"] = mode if eff == "pipeline" else res.mode
                row.pop("raw_response", None)
                row["_raw_len"] = len(res.raw_response)
                if args.verbose:
                    row["raw_response"] = res.raw_response
                all_results.append(row)
                if isinstance(res.parsed, dict) and not res.parsed.get("_parse_failed"):
                    page_parsed.append(res.parsed)

            if eff == "pipeline" and page_parsed:
                htr = merge_transcribe_pages(page_parsed)
                log.info("─" * 72)
                log.info("HTR MERGED (%d Seiten, confidence=%.2f):", len(page_parsed), htr.get("htr_confidence", 0))
                log.info("%s", json.dumps(htr, ensure_ascii=False, indent=2)[:3000])
                transcript = htr.get("volltext") or ""
                npred = args.num_predict or DEFAULT_NUM_PREDICT["extract"]
                extracted, _, elapsed = run_extract(transcript, args.extract_model, npred)
                normalized = normalize_extracted_schulbericht(extracted, htr=htr)
                vision = schulbericht_to_vision_meta(normalized)
                log.info("PIPELINE EXTRACT (%.1fs):", elapsed)
                log.info("%s", json.dumps(normalized, ensure_ascii=False, indent=2))
                log.info("Vision-Meta: %s", json.dumps(vision, ensure_ascii=False, indent=2))
                all_results.append({
                    "mode": "pipeline_merged",
                    "parsed": normalized,
                    "htr": htr,
                    "vision_meta": vision,
                })
            elif eff == "schulbericht-e2e" and page_parsed:
                merged = merge_schulbericht_pages(page_parsed)
                vision = schulbericht_to_vision_meta(merged)
                log.info("─" * 72)
                log.info("SCHULBERICHT-E2E MERGED (%d Seiten):", len(page_parsed))
                log.info("%s", json.dumps(merged, ensure_ascii=False, indent=2))
                log.info("Vision-Meta: %s", json.dumps(vision, ensure_ascii=False, indent=2))
                all_results.append({
                    "mode": "schulbericht-e2e_merged",
                    "parsed": merged,
                    "vision_meta": vision,
                })
            elif eff == "transcribe" and page_parsed:
                merged = merge_transcribe_pages(page_parsed)
                log.info("─" * 72)
                log.info("HTR MERGED (%d Seiten, confidence=%.2f):", len(page_parsed), merged.get("htr_confidence", 0))
                log.info("%s", json.dumps(merged, ensure_ascii=False, indent=2))
                all_results.append({"mode": "transcribe_merged", "parsed": merged})

        summary = {
            "input": str(input_path),
            "media_root": str(MEDIA_ROOT),
            "model": args.model,
            "ollama_base": OLLAMA_BASE,
            "runs": all_results,
        }

        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("Ergebnis gespeichert: %s", out)

        errors = sum(1 for r in all_results if r.get("error"))
        if errors:
            log.warning("%d von %d Läufen fehlgeschlagen", errors, len(all_results))
            return 1
        return 0
    finally:
        if resolved.temp_pdf and resolved.temp_pdf.is_file():
            resolved.temp_pdf.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
