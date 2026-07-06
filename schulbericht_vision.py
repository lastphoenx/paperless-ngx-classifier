"""Mehrseitige Schulbericht-Vision — HTR + Extraktion (post_consume + CLI)."""
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
HTR_NUM_PREDICT = int(os.environ.get("SCHULBERICHT_HTR_NUM_PREDICT", "2048"))
SCHULBERICHT_DPI = int(os.environ.get("SCHULBERICHT_DPI", "220"))

SCHULBERICHT_VISION_SYSTEM = (
    "Du bist ein JSON-Extraktor für Schweizer Schulberichte. "
    "Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt. "
    "Kein Text davor oder danach. Kein Markdown."
)
HTR_VISION_SYSTEM = (
    "Du bist ein vorsichtiger HTR-Transkriptor für deutsche Handschrift. "
    "Deine Aufgabe ist ABSCHREIBEN, nicht interpretieren. "
    "Korrigiere keine Grammatik, ersetze keine Wörter durch plausiblere Wörter. "
    "Bei Unsicherheit schreibe [?] direkt hinter das unsichere Wort. "
    "Bei unlesbaren Wörtern schreibe [unleserlich]. "
    "Antworte nur mit validem JSON."
)
EXTRACT_SYSTEM = (
    "Du extrahierst strukturierte Felder aus einer zeilengetreuen Transkription. "
    "Nutze nur den gegebenen Text. Erfinde nichts. "
    "Antworte AUSSCHLIESSLICH mit validem JSON."
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
    return s in ("", "null", "none", "nicht verfügbar", "nicht angegeben", "n/a")


def build_schulbericht_vision_prompt(
    ocr_text: str,
    page: int = 1,
    page_total: int = 1,
) -> str:
    """E2E-Prompt (Debug/Vergleich — interpretiert, nicht zeilengetreu)."""
    ocr_snip = (ocr_text or "")[:2000]
    page_hint = ""
    if page_total > 1:
        page_hint = (
            f"SEITE {page} von {page_total}.\n"
            f"{'Kopfzeile/Schülerdaten oft auf Seite 1.' if page == 1 else 'Fortsetzung.'}\n"
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
        '  "semester_oder_zeitraum": "string oder null",\n'
        '  "schule": "string oder null",\n'
        '  "lehrperson": "string oder null",\n'
        '  "arbeits_haltung": "Kurzfassung oder null",\n'
        '  "leistungen": "Kurzfassung oder null",\n'
        '  "handschrift_lesbarkeit": "gut|mittel|schlecht",\n'
        '  "confidence": 0.0,\n'
        '  "hinweise": "Unsicherheiten oder null"\n'
        "}\n\n"
        f"OCR-Text (meist leer bei Handschrift):\n{ocr_snip or '(leer)'}"
    )


def build_htr_transcribe_prompt(page: int = 1, page_total: int = 1) -> str:
    page_hint = f"SEITE {page} von {page_total}.\n" if page_total > 1 else ""
    return (
        f"{page_hint}"
        "Transkribiere den sichtbaren Text ZEILENGETREU in Lesereihenfolge.\n"
        "Wichtig:\n"
        "- Schreibe jede sichtbare Zeile einzeln ab.\n"
        "- Schreibe ab, interpretiere nicht. Ersetze unklare Wörter nicht durch plausible Wörter.\n"
        "- Erfinde keine fehlenden Wörter.\n"
        "- Fasse nichts zusammen.\n"
        "- Normalisiere keine Rechtschreibung.\n"
        "- Wenn ein Wort unsicher ist, markiere es mit [?].\n"
        "- Wenn ein Wort nicht lesbar ist, schreibe [unleserlich].\n"
        "- Trenne gedruckten Text und handschriftlichen Text, wenn erkennbar.\n\n"
        "Antworte exakt als JSON:\n"
        "{\n"
        f'  "seite": {page},\n'
        '  "gedruckt": ["..."],\n'
        '  "handschrift_zeilen": ["...", "..."],\n'
        '  "unsichere_woerter": ["..."],\n'
        '  "unleserliche_stellen": 0,\n'
        '  "qualitaet": "gut|mittel|schlecht"\n'
        "}\n"
    )


def build_extract_from_transcript_prompt(transcript: str) -> str:
    return (
        "Extrahiere aus dieser zeilengetreuen Transkription eines Schulberichts "
        "die folgenden Felder. Nutze nur den gegebenen Text. Erfinde nichts.\n"
        "Wenn ein Feld im Text nicht vorkommt: null.\n\n"
        "Antworte als JSON:\n"
        "{\n"
        '  "dokumenttyp": "Schulbericht",\n'
        '  "schueler_vorname": null,\n'
        '  "schueler_nachname": null,\n'
        '  "klasse": null,\n'
        '  "semester_oder_zeitraum": null,\n'
        '  "schule": null,\n'
        '  "lehrperson": null,\n'
        '  "arbeitshaltung": null,\n'
        '  "leistungen": null,\n'
        '  "offene_unsicherheiten": []\n'
        "}\n\n"
        f"TRANSKRIPTION:\n{transcript[:12000]}"
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


def merge_htr_transcribe_pages(pages: list[dict]) -> dict:
    """Nur Zeilen mergen — keine Feld-Fusion aus Einzelseiten."""
    if not pages:
        return {}
    printed: list[str] = []
    lines: list[str] = []
    uncertain: list[str] = []
    unles = 0
    for p in pages:
        for item in p.get("gedruckt") or []:
            if item and str(item).strip():
                printed.append(str(item).strip())
        for item in p.get("handschrift_zeilen") or []:
            if item and str(item).strip():
                lines.append(str(item).strip())
        uncertain.extend(p.get("unsichere_woerter") or [])
        unles += int(p.get("unleserliche_stellen") or 0)
        if not lines and p.get("volltext"):
            lines.extend(str(p["volltext"]).splitlines())
    volltext = "\n".join([*printed, *lines])
    return {
        "gedruckt": printed,
        "handschrift_zeilen": lines,
        "unsichere_woerter": uncertain,
        "unleserliche_stellen": unles,
        "volltext": volltext,
        "seiten_anzahl": len(pages),
        "qualitaet": pages[-1].get("qualitaet") if pages else "schlecht",
    }


def estimate_htr_confidence(htr: dict) -> float:
    """Eigene Confidence — nicht die (zu optimistische) Modell-Angabe."""
    lines = htr.get("handschrift_zeilen") or []
    text = htr.get("volltext") or "\n".join(lines)
    if not text.strip():
        return 0.0
    tokens = text.split()
    if not tokens:
        return 0.0
    markers = text.count("[?]") + text.count("[unleserlich]")
    unles = int(htr.get("unleserliche_stellen") or 0)
    penalty = min(0.65, (markers + unles) / max(1, len(tokens)) * 4)
    base = {"gut": 0.75, "mittel": 0.55, "schlecht": 0.35}.get(
        str(htr.get("qualitaet") or "mittel").lower(), 0.5,
    )
    return round(max(0.05, base - penalty), 2)


def normalize_extracted_schulbericht(
    data: dict,
    *,
    htr: dict | None = None,
    seiten: int | None = None,
) -> dict:
    """Extract-JSON → einheitliches Schulbericht-Format für schulbericht_to_vision_meta."""
    offen = data.get("offene_unsicherheiten") or []
    hinweise = ", ".join(str(x) for x in offen) if isinstance(offen, list) and offen else None
    conf = estimate_htr_confidence(htr) if htr else None
    return {
        "dokumenttyp": data.get("dokumenttyp") or "Schulbericht",
        "schueler_vorname": data.get("schueler_vorname"),
        "schueler_nachname": data.get("schueler_nachname"),
        "klasse": data.get("klasse"),
        "semester_oder_zeitraum": data.get("semester_oder_zeitraum"),
        "schule": data.get("schule"),
        "lehrperson": data.get("lehrperson"),
        "arbeits_haltung": data.get("arbeitshaltung") or data.get("arbeits_haltung"),
        "leistungen": data.get("leistungen"),
        "hinweise": hinweise,
        "confidence": conf,
        "seiten_anzahl": seiten or (htr or {}).get("seiten_anzahl"),
        "_htr": htr,
        "_extract": data,
    }


def merge_schulbericht_pages(pages: list[dict]) -> dict:
    """E2E-Merge (Debug): Metadaten nur von Seite 1, Textfelder anhängen."""
    if not pages:
        return {}
    merged: dict = {}
    meta_keys = (
        "schueler_vorname", "schueler_nachname", "klasse",
        "semester_oder_zeitraum", "schule", "lehrperson", "dokumenttyp",
    )
    for key in meta_keys:
        for p in pages:
            val = p.get(key)
            if not _nullish(val):
                merged[key] = val
                break

    text_fields = ("arbeits_haltung", "leistungen", "hinweise")
    for key in text_fields:
        parts = [
            str(p[key]).strip()
            for p in pages
            if not _nullish(p.get(key))
        ]
        if parts:
            merged[key] = " ".join(parts)

    merged["seiten_anzahl"] = len(pages)
    merged["dokumenttyp"] = merged.get("dokumenttyp") or "Schulbericht"
    merged["handschrift_lesbarkeit"] = pages[0].get("handschrift_lesbarkeit", "mittel")
    merged["confidence"] = 0.5
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
    ah = sb.get("arbeits_haltung") or sb.get("arbeitshaltung")
    if not _nullish(ah):
        summary_parts.append(f"Arbeitshaltung: {ah}")
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
    if sb.get("confidence") is not None:
        meta["schulbericht_confidence"] = sb["confidence"]
    meta["_schulbericht"] = sb
    return meta


def _analyze_pdf_pages(
    pdf_path: str,
    label: str,
    *,
    pdf_to_b64: Callable[[str, int], Optional[str]],
    page_analyze: Callable[[str, int, int], dict],
) -> list[dict]:
    total = pdf_page_count(pdf_path)
    log.info("%s: %d Seite(n) in %s", label, total, pdf_path)
    pages: list[dict] = []
    for page in range(1, total + 1):
        b64 = pdf_to_b64(pdf_path, page)
        if not b64:
            log.warning("%s Seite %d/%d: kein Bild", label, page, total)
            continue
        data = page_analyze(b64, page, total)
        if data:
            pages.append(data)
            log.info("%s Seite %d/%d: %s", label, page, total, json.dumps(data, ensure_ascii=False)[:200])
    return pages


def analyze_schulbericht_pdf(
    pdf_path: str,
    ocr_text: str,
    *,
    pdf_to_b64: Callable[[str, int], Optional[str]],
    vision_page: Callable[[str, str, int, int], dict],
) -> dict:
    """E2E: alle Seiten direkt → Schulbericht-JSON (nur Debug/Vergleich)."""

    def _page(b64: str, page: int, total: int) -> dict:
        return vision_page(b64, ocr_text, page, total)

    pages = _analyze_pdf_pages(
        pdf_path, "Schulbericht-E2E",
        pdf_to_b64=pdf_to_b64,
        page_analyze=lambda b64, p, t: _page(b64, p, t),
    )
    merged = merge_schulbericht_pages(pages)
    if merged:
        log.info(
            "Schulbericht-E2E merged: %s %s, Klasse=%s",
            merged.get("schueler_vorname"), merged.get("schueler_nachname"), merged.get("klasse"),
        )
    return merged


def analyze_schulbericht_two_stage(
    pdf_path: str,
    *,
    pdf_to_b64: Callable[[str, int], Optional[str]],
    htr_page: Callable[[str, int, int], dict],
    extract_from_text: Callable[[str], dict],
) -> dict:
    """Produktiv: HTR aller Seiten → Text-Extraktion (ohne Bild)."""
    pages = _analyze_pdf_pages(
        pdf_path, "Schulbericht-HTR",
        pdf_to_b64=pdf_to_b64,
        page_analyze=htr_page,
    )
    htr = merge_htr_transcribe_pages(pages)
    transcript = (htr.get("volltext") or "").strip()
    if not transcript:
        log.warning("Schulbericht-HTR: leere Transkription")
        return {}

    htr_conf = estimate_htr_confidence(htr)
    log.info("HTR merged: %d Zeilen, confidence=%.2f", len(htr.get("handschrift_zeilen") or []), htr_conf)

    extracted = extract_from_text(transcript)
    if not extracted:
        log.warning("Schulbericht-Extract: leeres JSON")
        return {}

    normalized = normalize_extracted_schulbericht(
        extracted, htr=htr, seiten=htr.get("seiten_anzahl"),
    )
    log.info(
        "Schulbericht-Extract: %s %s, Klasse=%s, confidence=%.2f",
        normalized.get("schueler_vorname"),
        normalized.get("schueler_nachname"),
        normalized.get("klasse"),
        normalized.get("confidence") or 0,
    )
    return normalized
