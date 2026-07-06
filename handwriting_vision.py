"""Handschrift-Erkennung (HTR): Profil-Routing, Default- und Schulbericht-Pipeline."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from schulbericht_vision import (
    _analyze_pdf_pages,
    analyze_schulbericht_pdf,
    analyze_schulbericht_two_stage,
    estimate_htr_confidence,
    looks_like_schulbericht,
    merge_htr_transcribe_pages,
    pdf_page_count,
    schulbericht_to_vision_meta,
)

log = logging.getLogger(__name__)

HTR_PROFILES = frozenset({"default", "schulbericht"})
HTR_PROFILE_AUTO = "auto"
HTR_PROFILE_OFF = "off"

_DOCUMENT_TYPES_JSON = Path(
    os.environ.get("DOCUMENT_TYPES_JSON", "/opt/paperless-scripts/training/document_types.json")
)
_HTR_PROFILE_MAP: dict[str, str] = {}
_HTR_PROFILE_LOADED = False


def _load_htr_profile_map() -> None:
    global _HTR_PROFILE_LOADED
    if _HTR_PROFILE_LOADED:
        return
    try:
        if _DOCUMENT_TYPES_JSON.exists():
            data = json.loads(_DOCUMENT_TYPES_JSON.read_text(encoding="utf-8"))
            for t in data.get("typen", []):
                name = (t.get("name") or "").strip().lower()
                prof = (t.get("htr_profile") or HTR_PROFILE_AUTO).strip().lower()
                if name:
                    _HTR_PROFILE_MAP[name] = prof
                for syn in t.get("synonyme", []) or []:
                    s = str(syn).strip().lower()
                    if s:
                        _HTR_PROFILE_MAP[s] = prof
        _HTR_PROFILE_LOADED = True
    except Exception as e:
        log.warning("htr_profile Map laden fehlgeschlagen: %s", e)
        _HTR_PROFILE_LOADED = True


def get_htr_profile_config(doctype_name: str) -> str:
    """Konfiguration aus document_types.json: auto | default | schulbericht | off."""
    if not doctype_name:
        return HTR_PROFILE_AUTO
    _load_htr_profile_map()
    return _HTR_PROFILE_MAP.get(doctype_name.strip().lower(), HTR_PROFILE_AUTO)


def detect_handwriting_signals(vision_meta: dict | None, ocr_text: str = "") -> bool:
    """Heuristik: Dokument enthält relevante Handschrift (ohne Schulbericht-Spezialfall)."""
    if looks_like_schulbericht(vision_meta, ocr_text):
        return True
    if not vision_meta:
        return False
    layout = str(vision_meta.get("layout") or "").lower()
    if "handgeschrieb" in layout:
        return True
    hs = vision_meta.get("handschrift")
    if hs and str(hs).strip().lower() not in ("", "null", "none", "keine"):
        return False  # Baseline-Vision hat kurze Notiz — kein Voll-HTR nötig
    ocr_len = len((ocr_text or "").strip())
    if ocr_len < 120 and "handgeschrieb" in layout:
        return True
    if ocr_len < 80 and any(k in layout for k in ("handschrift", "notiz", "manuell")):
        return True
    return False


def resolve_htr_profile(
    vision_meta: dict | None,
    ocr_text: str = "",
    *,
    doctype_name: str | None = None,
    explicit: str | None = None,
) -> str | None:
    """
    Welches HTR-Profil laufen soll, oder None (nur Baseline-Vision).

    Priorität: explicit → document_types (doctype_name) → document_types (dokumenttyp_visuell)
    → Auto (schulbericht-Heuristik, dann default-Heuristik).
    """
    if explicit:
        p = explicit.strip().lower()
        if p in (HTR_PROFILE_OFF, "none", "false", "0"):
            return None
        if p in HTR_PROFILES:
            return p

    for dt_candidate in filter(None, [
        doctype_name,
        (vision_meta or {}).get("dokumenttyp_visuell"),
    ]):
        cfg = get_htr_profile_config(str(dt_candidate))
        if cfg == HTR_PROFILE_OFF:
            return None
        if cfg in HTR_PROFILES:
            return cfg

    if looks_like_schulbericht(vision_meta, ocr_text):
        return "schulbericht"
    if detect_handwriting_signals(vision_meta, ocr_text):
        return "default"
    return None


def default_htr_to_vision_meta(htr: dict) -> dict:
    """Stufe-1-Transkript → vision_meta-Felder für post_consume."""
    if not htr:
        return {}
    lines = htr.get("handschrift_zeilen") or []
    printed = htr.get("gedruckt") or []
    handschrift = "\n".join(lines).strip()
    if not handschrift:
        handschrift = "\n".join(printed).strip()
    meta: dict = {
        "htr_profile": "default",
        "htr_confidence": estimate_htr_confidence(htr),
        "htr_volltext": htr.get("volltext"),
        "_htr": htr,
    }
    if handschrift:
        meta["handschrift"] = handschrift[:4000]
    if printed and not meta.get("handschrift"):
        meta["handschrift"] = "\n".join(printed)[:4000]
    return meta


def analyze_default_htr_pdf(
    pdf_path: str,
    *,
    pdf_to_b64: Callable[[str, int], Optional[str]],
    htr_page: Callable[[str, int, int], dict],
) -> dict:
    """Default-Profil: nur zeilengetreue HTR (Stufe 1), alle Seiten."""
    pages = _analyze_pdf_pages(
        pdf_path,
        "HTR-default",
        pdf_to_b64=pdf_to_b64,
        page_analyze=htr_page,
    )
    total = pdf_page_count(pdf_path)
    return merge_htr_transcribe_pages(pages, pages_total=total)


@dataclass
class HtrPipelineDeps:
    """Injizierte Callbacks aus post_consume (Ollama, PDF-Rendering)."""

    pdf_to_b64: Callable[[str, int], Optional[str]]
    htr_page: Callable[[str, int, int], dict]
    schulbericht_page_e2e: Callable[[str, str, int, int], dict]
    extract_schulbericht: Callable[[str], dict]


def run_htr_pipeline(
    profile: str,
    pdf_path: str,
    ocr_text: str,
    deps: HtrPipelineDeps,
) -> dict:
    """
    Mehrstufige Handschrift-Pipeline. Gibt vision_meta-Ergänzung zurück (leer = Fehler).
    """
    profile = (profile or "").strip().lower()
    if profile not in HTR_PROFILES:
        log.warning("Unbekanntes HTR-Profil: %s", profile)
        return {}

    if profile == "schulbericht":
        sb = analyze_schulbericht_two_stage(
            pdf_path,
            pdf_to_b64=deps.pdf_to_b64,
            htr_page=deps.htr_page,
            extract_from_text=deps.extract_schulbericht,
        )
        if not sb:
            log.warning("Schulbericht HTR/Extract leer — Fallback E2E")
            sb = analyze_schulbericht_pdf(
                pdf_path,
                ocr_text,
                pdf_to_b64=deps.pdf_to_b64,
                vision_page=deps.schulbericht_page_e2e,
            )
        return schulbericht_to_vision_meta(sb) if sb else {}

    htr = analyze_default_htr_pdf(
        pdf_path,
        pdf_to_b64=deps.pdf_to_b64,
        htr_page=deps.htr_page,
    )
    if not (htr.get("volltext") or "").strip():
        log.warning("Default-HTR: leere Transkription")
        return {}
    return default_htr_to_vision_meta(htr)


def format_htr_note_summary(meta: dict) -> str:
    """Kurzfassung für Paperless-Notiz nach nachträglicher HTR."""
    prof = meta.get("htr_profile") or meta.get("_schulbericht") and "schulbericht" or "?"
    lines = [
        f"[paper.manager HTR — Profil: {prof}]",
    ]
    if meta.get("htr_confidence") is not None:
        lines.append(f"Confidence: {meta['htr_confidence']}")
    if meta.get("schulbericht_confidence") is not None:
        lines.append(f"Confidence: {meta['schulbericht_confidence']}")
    for key, label in [
        ("schueler_vorname", "Vorname"),
        ("schueler_nachname", "Nachname"),
        ("klasse", "Klasse"),
        ("schule", "Schule"),
        ("lehrperson", "Lehrperson"),
    ]:
        if meta.get(key):
            lines.append(f"{label}: {meta[key]}")
    voll = meta.get("htr_volltext") or ""
    if not voll and meta.get("_htr"):
        voll = (meta["_htr"] or {}).get("volltext") or ""
    if not voll and meta.get("_schulbericht"):
        sb = meta["_schulbericht"] or {}
        voll = sb.get("volltext") or sb.get("arbeitshaltung") or ""
    if voll:
        snippet = str(voll).strip()
        if len(snippet) > 2500:
            snippet = snippet[:2500] + "\n…"
        lines.append("")
        lines.append(snippet)
    return "\n".join(lines)
