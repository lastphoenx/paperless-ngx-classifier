"""Mehrseitige Schulbericht-Vision — gemeinsam für post_consume und CLI-Test."""
from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Callable, Optional

log = logging.getLogger(__name__)

SCHULBERICHT_NUM_PREDICT = int(
    os.environ.get("SCHULBERICHT_VISION_NUM_PREDICT", "800")
)
SCHULBERICHT_VISION_SYSTEM = (
    "Du bist ein JSON-Extraktor für Schweizer Schulberichte. "
    "Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt. "
    "Kein Text davor oder danach. Kein Markdown."
)


def pdf_page_count(pdf_path: str) -> int:
    try:
        r = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        for line in r.stdout.splitlines():
            if line.startswith("Pages:"):
                return max(1, int(line.split(":", 1)[1].strip()))
    except Exception as e:
        log.warning("pdf_page_count fehlgeschlagen (%s): %s", pdf_path, e)
    return 1


def _nullish(val: object) -> bool:
    if val is None:
        return True
    s = str(val).strip().lower()
    return s in ("", "null", "none")


def build_schulbericht_vision_prompt(
    ocr_text: str,
    page: int = 1,
    page_total: int = 1,
) -> str:
    ocr_snip = (ocr_text or "")[:2000]
    page_hint = ""
    if page_total > 1:
        page_hint = (
            f"SEITE {page} von {page_total}.\n"
            f"{'Kopfzeile/Schülerdaten oft auf Seite 1.' if page == 1 else 'Fortsetzung — vor allem Leistungen/Fächer.'}\n"
        )
    return (
        f"{page_hint}"
        "Dies ist ein Schweizer Schulbericht oder Zeugnis-Auszug (oft handschriftlich).\n"
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
        '  "leistungen": "Kurzfassung Abschnitt Leistungen/Fächer oder null",\n'
        '  "handschrift_lesbarkeit": "gut|mittel|schlecht",\n'
        '  "confidence": 0.0,\n'
        '  "hinweise": "Unsicherheiten oder null"\n'
        "}\n\n"
        f"OCR-Text (meist unbrauchbar bei Handschrift):\n{ocr_snip or '(leer)'}"
    )


def looks_like_schulbericht(vision_meta: dict | None, ocr_text: str = "") -> bool:
    if not vision_meta:
        return False
    vis = str(vision_meta.get("dokumenttyp_visuell") or "").lower()
    layout = str(vision_meta.get("layout") or "").lower()
    if any(k in vis for k in ("schulbericht", "zeugnis", "schulzeugnis")):
        return True
    if "schulbericht" in layout:
        return True
    if "handgeschrieb" in layout and any(k in vis for k in ("schul", "bericht")):
        return True
    ocr_len = len((ocr_text or "").strip())
    if ocr_len < 80 and "handgeschrieb" in layout:
        return True
    return False


def merge_schulbericht_pages(pages: list[dict]) -> dict:
    if not pages:
        return {}
    merged: dict = {}
    text_fields = ("arbeits_haltung", "leistungen", "hinweise")
    text_parts: dict[str, list[str]] = {k: [] for k in text_fields}

    for p in pages:
        for key, val in p.items():
            if _nullish(val):
                continue
            if key in text_fields:
                text_parts[key].append(str(val).strip())
            elif key not in merged or _nullish(merged.get(key)):
                merged[key] = val

    for key in text_fields:
        parts = text_parts[key]
        if parts:
            merged[key] = " ".join(parts)

    confs = [float(p["confidence"]) for p in pages if isinstance(p.get("confidence"), (int, float))]
    if confs:
        merged["confidence"] = round(sum(confs) / len(confs), 2)

    merged["seiten_anzahl"] = len(pages)
    merged["dokumenttyp"] = merged.get("dokumenttyp") or "Schulbericht"
    return merged


def schulbericht_to_vision_meta(sb: dict) -> dict:
    """Schulbericht-JSON → Standard-Vision-Felder für Pipeline/LLM."""
    vor = (sb.get("schueler_vorname") or "").strip()
    nach = (sb.get("schueler_nachname") or "").strip()
    name = f"{vor} {nach}".strip()
    klass = sb.get("klasse") or ""
    sem = sb.get("semester_oder_zeitraum") or ""
    if _nullish(sem):
        sem = ""

    summary_parts: list[str] = []
    if not _nullish(sb.get("arbeits_haltung")):
        summary_parts.append(f"Arbeitshaltung: {sb['arbeits_haltung']}")
    if not _nullish(sb.get("leistungen")):
        summary_parts.append(f"Leistungen: {sb['leistungen']}")

    seiten = sb.get("seiten_anzahl") or 1
    meta: dict = {
        "dokumenttyp_visuell": "Schulbericht",
        "empfaenger": name or None,
        "layout": f"Handgeschriebener Schulbericht ({seiten} Seite(n))",
        "sprache": "de",
        "logo_vorhanden": False,
        "tabellen_vorhanden": False,
        "qr_einzahlungsschein": False,
    }
    if not _nullish(sb.get("schule")):
        meta["absender"] = str(sb["schule"]).strip()
    if summary_parts:
        meta["besonderheiten"] = "\n\n".join(summary_parts)
    if klass or sem:
        titel_bits = [b for b in (klass, sem) if b and not _nullish(b)]
        if titel_bits:
            meta["schulbericht_zeitraum"] = " — ".join(str(b) for b in titel_bits)
    meta["_schulbericht"] = sb
    return meta


def analyze_schulbericht_pdf(
    pdf_path: str,
    ocr_text: str,
    *,
    pdf_to_b64: Callable[[str, int], Optional[str]],
    vision_page: Callable[[str, str, int, int], dict],
) -> dict:
    """Alle PDF-Seiten einzeln analysieren und zu einem Schulbericht-JSON mergen."""
    total = pdf_page_count(pdf_path)
    log.info("Schulbericht-Vision: %d Seite(n) in %s", total, pdf_path)
    pages: list[dict] = []
    for page in range(1, total + 1):
        b64 = pdf_to_b64(pdf_path, page)
        if not b64:
            log.warning("Schulbericht Seite %d/%d: kein Bild", page, total)
            continue
        data = vision_page(b64, ocr_text, page, total)
        if data:
            pages.append(data)
            log.info(
                "Schulbericht Seite %d/%d: %s",
                page, total,
                json.dumps(data, ensure_ascii=False)[:240],
            )
    merged = merge_schulbericht_pages(pages)
    if merged:
        log.info(
            "Schulbericht merged (%d Seiten): Schüler=%s %s, Klasse=%s",
            merged.get("seiten_anzahl", 0),
            merged.get("schueler_vorname", "?"),
            merged.get("schueler_nachname", "?"),
            merged.get("klasse", "?"),
        )
    return merged
