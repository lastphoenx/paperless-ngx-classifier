#!/usr/bin/env python3
"""
CLI: Handschrift / Schulbericht — Vision-LLM (qwen2.5vl) testen und tunen.

Nur Kommandozeile — kein UI. Vergleicht Prompt-Varianten mit detaillierten Logs.

CT121 Beispiele:
  /opt/paperless-scripts/venv/bin/python3 scripts/test_handwriting_vision.py \\
    --doc-id 3577 --mode all -v

  /opt/paperless-scripts/venv/bin/python3 scripts/test_handwriting_vision.py \\
    /pfad/zum/scan.pdf --mode schulbericht --page 1 --num-predict 1024

  OLLAMA_MODEL_VISION=qwen2.5vl:7b ./scripts/test_handwriting_vision.py bild.png \\
    --mode transcribe --output /tmp/htr-result.json

Modi:
  baseline     — aktueller Pipeline-Prompt (Rechnungen + Rand-Handschrift)
  schulbericht — strukturierte Extraktion für Schulberichte
  transcribe   — wörtliche Handschrift-Transkription (HTR)
  all          — alle drei nacheinander
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

log = logging.getLogger("test_handwriting_vision")

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL_VISION = os.environ.get("OLLAMA_MODEL_VISION", "qwen2.5vl:7b")
VISION_TIMEOUT = int(os.environ.get("VISION_TIMEOUT", "120"))
MEDIA_ROOT = Path(os.environ.get(
    "PAPERLESS_MEDIA_ROOT",
    os.environ.get("MEDIA_ROOT", "/usr/src/paperless/media"),
))

VISION_SYSTEM_JSON = (
    "Du bist ein JSON-Extraktor für Schweizer Dokumente. "
    "Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt. "
    "Kein Text davor oder danach. Kein Markdown."
)
VISION_SYSTEM_HTR = (
    "Du transkribierst handschriftlichen Text auf Deutsch. "
    "Lies Buchstaben sorgfältig; erfinde nichts. "
    "Antworte AUSSCHLIESSLICH mit validem JSON wie im Prompt beschrieben."
)

MODES = ("baseline", "schulbericht", "transcribe", "all")
DEFAULT_NUM_PREDICT = {
    "baseline": 300,
    "schulbericht": 800,
    "transcribe": 2048,
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


def find_pdf_by_doc_id(doc_id: str | int) -> Optional[Path]:
    padded = str(doc_id).zfill(7)
    for sub in ("originals", "archive"):
        p = MEDIA_ROOT / "documents" / sub / f"{padded}.pdf"
        if p.is_file():
            return p
    return None


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


def build_schulbericht_prompt(ocr_text: str) -> str:
    ocr_snip = (ocr_text or "")[:2000]
    return (
        "Dies ist vermutlich ein Schweizer Schulbericht oder Zeugnis-Auszug.\n"
        "Lies gedruckte UND handschriftliche Inhalte vom Bild.\n"
        "Fasse lange Handschrift-Absätze sinnvoll zusammen (kein Wort-für-Wort nötig).\n"
        "Antworte als JSON:\n"
        "{\n"
        '  "dokumenttyp": "Schulbericht",\n'
        '  "schueler_vorname": "string oder null",\n'
        '  "schueler_nachname": "string oder null",\n'
        '  "klasse": "string oder null",\n'
        '  "semester_oder_zeitraum": "z.B. 1. Semester 2025/26 oder null",\n'
        '  "schule": "Name der Schule oder null",\n'
        '  "lehrperson": "string oder null",\n'
        '  "arbeits_haltung": "Kurzfassung Abschnitt Arbeitshaltung oder null",\n'
        '  "leistungen": "Kurzfassung Abschnitt Leistungen oder null",\n'
        '  "handschrift_lesbarkeit": "gut|mittel|schlecht",\n'
        '  "confidence": 0.0,\n'
        '  "hinweise": "Unsicherheiten oder null"\n'
        "}\n\n"
        f"OCR-Text (meist unbrauchbar bei Handschrift):\n{ocr_snip or '(leer)'}"
    )


def build_transcribe_prompt(ocr_text: str) -> str:
    ocr_snip = (ocr_text or "")[:500]
    return (
        "Transkribiere ALLEN sichtbaren Text auf diesem Bild wörtlich auf Deutsch.\n"
        "Unterscheide gedruckte Überschriften und handschriftlichen Fliesstext.\n"
        "Erfinde keine Wörter. Bei unleserlichen Stellen: [unleserlich].\n"
        "Antworte als JSON:\n"
        "{\n"
        '  "abschnitte": [\n'
        '    {"titel": "Arbeitshaltung", "text": "...", "quelle": "handschrift|gedruckt"}\n'
        "  ],\n"
        '  "volltext": "kompletter Text in Lesereihenfolge",\n'
        '  "handschrift_anteil": "hoch|mittel|niedrig",\n'
        '  "qualitaet": "gut|mittel|schlecht"\n'
        "}\n\n"
        f"OCR-Referenz (oft leer):\n{ocr_snip or '(leer)'}"
    )


def prompt_for_mode(mode: str, ocr_text: str) -> tuple[str, str]:
    if mode == "baseline":
        return build_baseline_prompt(ocr_text), VISION_SYSTEM_JSON
    if mode == "schulbericht":
        return build_schulbericht_prompt(ocr_text), VISION_SYSTEM_JSON
    if mode == "transcribe":
        return build_transcribe_prompt(ocr_text), VISION_SYSTEM_HTR
    raise ValueError(f"Unbekannter Modus: {mode}")


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


def run_mode(
    mode: str,
    image_b64: str,
    image_bytes: int,
    ocr_text: str,
    page: int,
    model: str,
    num_predict: int,
    temperature: float,
) -> RunResult:
    user_prompt, system = prompt_for_mode(mode, ocr_text)
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


def parse_pages_arg(pages: Optional[str], page: int) -> list[int]:
    if pages:
        return [int(p.strip()) for p in pages.split(",") if p.strip()]
    return [page]


def resolve_input_path(path: Optional[str], doc_id: Optional[int]) -> Path:
    if path:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Datei nicht gefunden: {p}")
        return p
    if doc_id is not None:
        found = find_pdf_by_doc_id(doc_id)
        if found:
            log.info("Dok #%s → %s", doc_id, found)
            return found
        raise FileNotFoundError(
            f"PDF für Dok #{doc_id} nicht unter {MEDIA_ROOT}/documents/{{originals,archive}}/ gefunden"
        )
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
    parser.add_argument("--page", type=int, default=1, help="PDF-Seite 1-basiert (default: 1)")
    parser.add_argument("--pages", help="Mehrere Seiten, kommagetrennt z.B. 1,2,3")
    parser.add_argument("--dpi", type=int, default=150, help="Render-Auflösung (default: 150)")
    parser.add_argument("--model", default=MODEL_VISION, help=f"Ollama Vision-Modell (default: {MODEL_VISION})")
    parser.add_argument("--num-predict", type=int, help="Token-Limit (default je nach Modus)")
    parser.add_argument("--temperature", type=float, default=0.1)
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
        input_path = resolve_input_path(args.path, args.doc_id)
    except (FileNotFoundError, ValueError) as e:
        log.error("%s", e)
        return 2

    modes = list(MODES[:-1]) if args.mode == "all" else [args.mode]
    page_list = parse_pages_arg(args.pages, args.page)

    if args.ocr_file:
        ocr_full = Path(args.ocr_file).read_text(encoding="utf-8", errors="replace").strip()
    elif input_path.suffix.lower() == ".pdf":
        ocr_full = pdftotext(input_path)
    else:
        ocr_full = ""

    if args.ocr_chars and len(ocr_full) > args.ocr_chars:
        ocr_full = ocr_full[: args.ocr_chars]

    log.info(
        "Input=%s  Modi=%s  Seiten=%s  Modell=%s  DPI=%d  Ollama=%s",
        input_path, modes, page_list, args.model, args.dpi, OLLAMA_BASE,
    )
    if ocr_full:
        log.info("OCR (%d Zeichen): %s", len(ocr_full), ocr_full[:200].replace("\n", " ") + ("…" if len(ocr_full) > 200 else ""))
    else:
        log.info("OCR: (leer — typisch bei Handschrift-Scan)")

    all_results: list[dict] = []

    for page in page_list:
        try:
            image_b64, image_bytes = file_to_base64_image(input_path, page=page, dpi=args.dpi)
        except Exception as e:
            log.error("Bild-Rendering Seite %d fehlgeschlagen: %s", page, e)
            return 1
        log.info("Seite %d gerendert: %d Bytes JPEG (base64 %d Zeichen)", page, image_bytes, len(image_b64))

        for mode in modes:
            npred = args.num_predict if args.num_predict is not None else DEFAULT_NUM_PREDICT[mode]
            res = run_mode(
                mode=mode,
                image_b64=image_b64,
                image_bytes=image_bytes,
                ocr_text=ocr_full,
                page=page,
                model=args.model,
                num_predict=npred,
                temperature=args.temperature,
            )
            log_result(res)
            row = asdict(res)
            row.pop("raw_response", None)
            row["_raw_len"] = len(res.raw_response)
            if args.verbose:
                row["raw_response"] = res.raw_response
            all_results.append(row)

    summary = {
        "input": str(input_path),
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


if __name__ == "__main__":
    sys.exit(main())
