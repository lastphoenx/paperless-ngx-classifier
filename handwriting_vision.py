"""Handschrift-Erkennung (HTR): Profil-Routing, Pre-Resolution, Default- und Schulbericht-Pipeline."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from schulbericht_vision import (
    _analyze_pdf_pages,
    analyze_schulbericht_pdf,
    analyze_schulbericht_two_stage,
    clean_htr_lines,
    estimate_htr_confidence,
    looks_like_schulbericht,
    merge_htr_transcribe_pages,
    pdf_page_count,
    rebuild_htr_volltext,
    schulbericht_to_vision_meta,
)

log = logging.getLogger(__name__)

HTR_PROFILE_AUTO = "auto"
HTR_PROFILE_OFF = "off"
HTR_ACTION_RUN = "run_now"
HTR_ACTION_DEFER = "defer"
HTR_ACTION_SKIP = "skip"

_RUN_PROFILES = frozenset({"default", "schulbericht", "schulbericht_crop_strong"})

_DOCUMENT_TYPES_JSON = Path(
    os.environ.get("DOCUMENT_TYPES_JSON", "/opt/paperless-scripts/training/document_types.json")
)
_HTR_PROFILES_JSON = Path(
    os.environ.get(
        "HTR_PROFILES_JSON",
        str(Path(__file__).resolve().parent / "training" / "htr_profiles.json"),
    )
)

# synonym (lower) → canonical doctype key (type name lower)
_SYNONYM_TO_CANONICAL: dict[str, str] = {}
# canonical doctype key → htr_profile from document_types.json
_CANONICAL_TO_DT_PROFILE: dict[str, str] = {}
_DOCTYPE_MAP_LOADED = False

_HTR_REGISTRY: dict[str, "HtrProfileConfig"] = {}
_HTR_REGISTRY_LOADED = False

_DEFAULT_REGISTRY: dict[str, dict] = {
    "default": {"pipeline": "default", "crop_mode": "trim", "dpi": 220, "enhance": True},
    "schulbericht": {
        "pipeline": "schulbericht",
        "crop_mode": "horizontal",
        "dpi": 220,
        "horizontal_bands": [0.0, 0.34, 0.68, 1.0],
        "band_padding_px": 12,
        "enhance": True,
    },
    "schulbericht_crop_strong": {
        "pipeline": "schulbericht",
        "crop_mode": "horizontal",
        "dpi": 240,
        "horizontal_bands": [0.0, 0.30, 0.65, 1.0],
        "band_padding_px": 16,
        "enhance": True,
    },
}


@dataclass
class HtrProfileConfig:
    name: str
    pipeline: str = "default"
    crop_mode: str = "trim"
    dpi: int = 220
    enhance: bool = True
    horizontal_bands: list[float] = field(default_factory=lambda: [0.0, 0.34, 0.68, 1.0])
    band_padding_px: int = 12


@dataclass
class HtrPreResolution:
    action: str
    profile_name: str | None = None
    config: HtrProfileConfig | None = None
    profile_confidence: str = "low"
    crop_mode_effective: str = "trim"
    document_type_raw: str | None = None
    document_type_used: str | None = None
    document_type_source: str = "vision"
    htr_profile_source: str = "no_htr_signal"
    correspondent: str | None = None
    correspondent_match: str | None = None
    variants: dict = field(default_factory=dict)

    def to_audit_dict(self) -> dict:
        d = asdict(self)
        d.pop("config", None)
        if self.config:
            d["dpi"] = self.config.dpi
            d["pipeline"] = self.config.pipeline
        d["htr_executed"] = self.action == HTR_ACTION_RUN
        if self.action == HTR_ACTION_DEFER:
            d["pending_tag"] = "pending_htr_decision"
        return d


def _load_document_type_maps() -> None:
    global _DOCTYPE_MAP_LOADED
    if _DOCTYPE_MAP_LOADED:
        return
    try:
        if _DOCUMENT_TYPES_JSON.exists():
            data = json.loads(_DOCUMENT_TYPES_JSON.read_text(encoding="utf-8"))
            for t in data.get("typen", []):
                canonical = (t.get("name") or "").strip().lower()
                if not canonical:
                    continue
                prof = (t.get("htr_profile") or HTR_PROFILE_AUTO).strip().lower()
                _CANONICAL_TO_DT_PROFILE[canonical] = prof
                _SYNONYM_TO_CANONICAL[canonical] = canonical
                for syn in t.get("synonyme", []) or []:
                    s = str(syn).strip().lower()
                    if s:
                        _SYNONYM_TO_CANONICAL[s] = canonical
        _DOCTYPE_MAP_LOADED = True
    except Exception as e:
        log.warning("document_type Map laden fehlgeschlagen: %s", e)
        _DOCTYPE_MAP_LOADED = True


def _load_htr_registry() -> None:
    global _HTR_REGISTRY_LOADED
    if _HTR_REGISTRY_LOADED:
        return
    raw_profiles = dict(_DEFAULT_REGISTRY)
    try:
        if _HTR_PROFILES_JSON.exists():
            data = json.loads(_HTR_PROFILES_JSON.read_text(encoding="utf-8"))
            raw_profiles.update(data.get("profile") or {})
    except Exception as e:
        log.warning("htr_profiles.json laden fehlgeschlagen: %s", e)

    for name, cfg in raw_profiles.items():
        if not isinstance(cfg, dict):
            continue
        _HTR_REGISTRY[name.strip().lower()] = HtrProfileConfig(
            name=name.strip().lower(),
            pipeline=(cfg.get("pipeline") or "default").strip().lower(),
            crop_mode=(cfg.get("crop_mode") or "trim").strip().lower(),
            dpi=int(cfg.get("dpi") or 220),
            enhance=bool(cfg.get("enhance", True)),
            horizontal_bands=list(cfg.get("horizontal_bands") or [0.0, 0.34, 0.68, 1.0]),
            band_padding_px=int(cfg.get("band_padding_px") or 12),
        )
    _HTR_REGISTRY_LOADED = True


def get_htr_profile_config(name: str) -> HtrProfileConfig | None:
    _load_htr_registry()
    return _HTR_REGISTRY.get((name or "").strip().lower())


def list_htr_profile_names() -> list[str]:
    _load_htr_registry()
    return sorted(_HTR_REGISTRY.keys())


def normalize_document_type_key(raw: str | None) -> tuple[str | None, str]:
    """Vision-Rohname → canonical key. Returns (key, source)."""
    if not raw or not str(raw).strip():
        return None, "vision"
    _load_document_type_maps()
    key = str(raw).strip().lower()
    if key in _SYNONYM_TO_CANONICAL:
        canonical = _SYNONYM_TO_CANONICAL[key]
        source = "vision_synonym_map" if canonical != key else "vision"
        return canonical, source
    return key, "vision"


def get_document_type_htr_profile(canonical_type: str | None) -> str:
    if not canonical_type:
        return HTR_PROFILE_AUTO
    _load_document_type_maps()
    return _CANONICAL_TO_DT_PROFILE.get(canonical_type.strip().lower(), HTR_PROFILE_AUTO)


def effective_crop_mode(config: HtrProfileConfig, profile_confidence: str) -> str:
    mode = config.crop_mode
    if mode != "horizontal":
        return mode
    if profile_confidence == "high":
        return "horizontal"
    if profile_confidence == "medium" and config.pipeline == "schulbericht":
        return "horizontal"
    return "trim"


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
        return False
    ocr_len = len((ocr_text or "").strip())
    if ocr_len < 120 and "handgeschrieb" in layout:
        return True
    if ocr_len < 80 and any(k in layout for k in ("handschrift", "notiz", "manuell")):
        return True
    return False


def _resolution_run(
    profile_name: str,
    *,
    source: str,
    confidence: str,
    document_type_raw: str | None = None,
    document_type_used: str | None = None,
    document_type_source: str = "vision",
    correspondent: str | None = None,
    correspondent_match: str | None = None,
) -> HtrPreResolution:
    if profile_name == HTR_PROFILE_OFF:
        return HtrPreResolution(
            action=HTR_ACTION_SKIP,
            htr_profile_source="off",
            profile_confidence=confidence,
            document_type_raw=document_type_raw,
            document_type_used=document_type_used,
            document_type_source=document_type_source,
            correspondent=correspondent,
            correspondent_match=correspondent_match,
        )
    config = get_htr_profile_config(profile_name)
    if not config or config.pipeline == HTR_PROFILE_OFF:
        return HtrPreResolution(
            action=HTR_ACTION_SKIP,
            profile_name=profile_name,
            htr_profile_source=source,
            profile_confidence=confidence,
            document_type_raw=document_type_raw,
            document_type_used=document_type_used,
            document_type_source=document_type_source,
        )
    crop_eff = effective_crop_mode(config, confidence)
    return HtrPreResolution(
        action=HTR_ACTION_RUN,
        profile_name=profile_name,
        config=config,
        profile_confidence=confidence,
        crop_mode_effective=crop_eff,
        document_type_raw=document_type_raw,
        document_type_used=document_type_used,
        document_type_source=document_type_source,
        htr_profile_source=source,
        correspondent=correspondent,
        correspondent_match=correspondent_match,
    )


def decide_htr_action(
    vision_meta: dict | None,
    ocr_text: str = "",
    *,
    explicit: str | None = None,
    correspondent: dict | None = None,
    correspondent_match: str | None = None,
    document_type_key: str | None = None,
    document_type_source: str = "paperless",
) -> HtrPreResolution:
    """
    HTR-Pre-Resolution für Consume-Lauf (Vision) oder Reprocess (Paperless-Doctype).
    """
    corr_name = (correspondent or {}).get("name") if correspondent else None

    if explicit:
        p = explicit.strip().lower()
        if p in (HTR_PROFILE_OFF, "none", "false", "0"):
            return HtrPreResolution(action=HTR_ACTION_SKIP, htr_profile_source="explicit", profile_confidence="high")
        if p == HTR_PROFILE_AUTO:
            explicit = None
        elif p in _RUN_PROFILES or get_htr_profile_config(p):
            return _resolution_run(
                p, source="explicit", confidence="high", correspondent=corr_name,
                correspondent_match=correspondent_match,
            )

    if document_type_key:
        normalized = document_type_key.strip().lower()
        dt_source = document_type_source
        raw_vis = None
    else:
        raw_vis = (vision_meta or {}).get("dokumenttyp_visuell")
        normalized, dt_source = normalize_document_type_key(str(raw_vis) if raw_vis else None)

    # Korrespondent-Override (nur bei sicherem Match)
    if correspondent and normalized and correspondent_match in ("UID", "IBAN", "E-Mail", "paperless"):
        overrides = correspondent.get("htr_profiles_by_document_type") or {}
        override = (overrides.get(normalized) or "").strip().lower()
        if override:
            return _resolution_run(
                override,
                source="correspondent_document_type_override",
                confidence="high",
                document_type_raw=raw_vis if not document_type_key else document_type_key,
                document_type_used=normalized,
                document_type_source=dt_source,
                correspondent=corr_name,
                correspondent_match=correspondent_match,
            )

    dt_profile = get_document_type_htr_profile(normalized)
    if dt_profile in _RUN_PROFILES or (dt_profile and get_htr_profile_config(dt_profile)):
        return _resolution_run(
            dt_profile,
            source="document_type_default",
            confidence="high",
            document_type_raw=raw_vis if not document_type_key else document_type_key,
            document_type_used=normalized,
            document_type_source=dt_source,
            correspondent=corr_name,
            correspondent_match=correspondent_match,
        )

    if dt_profile == HTR_PROFILE_OFF:
        return HtrPreResolution(
            action=HTR_ACTION_SKIP,
            htr_profile_source="document_type_off",
            profile_confidence="high",
            document_type_raw=raw_vis,
            document_type_used=normalized,
            document_type_source=dt_source,
        )

    if looks_like_schulbericht(vision_meta, ocr_text):
        return _resolution_run(
            "schulbericht",
            source="auto_heuristic_schulbericht",
            confidence="medium",
            document_type_raw=raw_vis,
            document_type_used=normalized,
            document_type_source=dt_source,
        )

    if detect_handwriting_signals(vision_meta, ocr_text):
        return HtrPreResolution(
            action=HTR_ACTION_DEFER,
            profile_name="default",
            config=get_htr_profile_config("default"),
            profile_confidence="low",
            crop_mode_effective="trim",
            document_type_raw=raw_vis,
            document_type_used=normalized,
            document_type_source=dt_source,
            htr_profile_source="auto_heuristic_handwriting",
        )

    return HtrPreResolution(action=HTR_ACTION_SKIP, htr_profile_source="no_htr_signal")


def default_htr_to_vision_meta(htr: dict) -> dict:
    """Stufe-1-Transkript → vision_meta-Felder für post_consume."""
    if not htr:
        return {}
    lines = htr.get("handschrift_zeilen") or []
    printed = htr.get("gedruckt") or []
    handschrift = "\n".join(clean_htr_lines([str(x) for x in lines])).strip()
    if not handschrift:
        handschrift = rebuild_htr_volltext(htr)
    meta: dict = {
        "htr_profile": "default",
        "htr_confidence": estimate_htr_confidence(htr),
        "htr_volltext": rebuild_htr_volltext(htr) or htr.get("volltext"),
        "_htr": htr,
    }
    if handschrift:
        meta["handschrift"] = handschrift[:4000]
    elif printed:
        meta["handschrift"] = "\n".join(clean_htr_lines([str(x) for x in printed]))[:4000]
    return meta


def resolve_htr_profile(
    vision_meta: dict | None,
    ocr_text: str = "",
    *,
    doctype_name: str | None = None,
    explicit: str | None = None,
) -> str | None:
    """Legacy-API — delegiert an decide_htr_action."""
    resolution = decide_htr_action(
        vision_meta,
        ocr_text,
        explicit=explicit,
        document_type_key=doctype_name.strip().lower() if doctype_name else None,
        document_type_source="paperless",
    )
    if resolution.action != HTR_ACTION_RUN:
        return None
    return resolution.profile_name


def audit_missed_correspondent_override(
    pre_resolution: HtrPreResolution,
    final_correspondent: dict | None,
    *,
    document_type_used: str | None = None,
) -> dict | None:
    """Prüft ob finaler Korrespondent ein anderes HTR-Profil gehabt hätte (Audit only)."""
    if not final_correspondent or not document_type_used:
        return None
    if pre_resolution.correspondent_match in ("UID", "IBAN", "E-Mail"):
        return None
    overrides = final_correspondent.get("htr_profiles_by_document_type") or {}
    override = (overrides.get(document_type_used) or "").strip().lower()
    if not override:
        return None
    current = pre_resolution.profile_name or ""
    if override == current or (override == HTR_PROFILE_OFF and pre_resolution.action == HTR_ACTION_SKIP):
        return None
    would_profile = None if override == HTR_PROFILE_OFF else override
    delta = bool(would_profile and would_profile != current)
    return {
        "htr_correspondent_override_missed": True,
        "early_correspondent": pre_resolution.correspondent,
        "final_correspondent": final_correspondent.get("name"),
        "would_have_used_profile": would_profile,
        "htr_rerun_recommended": delta,
    }


def analyze_default_htr_pdf(
    pdf_path: str,
    *,
    resolution: HtrPreResolution,
    htr_page: Callable[..., dict],
) -> dict:
    pages, variants = _analyze_pdf_pages(
        pdf_path,
        "HTR-default",
        resolution=resolution,
        page_analyze=htr_page,
    )
    resolution.variants = variants
    total = pdf_page_count(pdf_path)
    return merge_htr_transcribe_pages(pages, pages_total=total)


@dataclass
class HtrPipelineDeps:
    pdf_to_b64: Callable[[str, int], Optional[str]]
    htr_page: Callable[..., dict]
    schulbericht_page_e2e: Callable[[str, str, int, int], dict]
    extract_schulbericht: Callable[[str], dict]


def run_htr_pipeline(
    resolution: HtrPreResolution | str,
    pdf_path: str,
    ocr_text: str,
    deps: HtrPipelineDeps,
) -> dict:
    """Mehrstufige Handschrift-Pipeline."""
    if isinstance(resolution, str):
        cfg = get_htr_profile_config(resolution)
        resolution = HtrPreResolution(
            action=HTR_ACTION_RUN,
            profile_name=resolution,
            config=cfg,
            profile_confidence="high",
            crop_mode_effective=effective_crop_mode(cfg, "high") if cfg else "trim",
            htr_profile_source="legacy_string",
        )

    if resolution.action != HTR_ACTION_RUN or not resolution.config:
        return {}

    pipeline = resolution.config.pipeline
    profile_name = resolution.profile_name or "default"

    if pipeline == "schulbericht":
        sb = analyze_schulbericht_two_stage(
            pdf_path,
            resolution=resolution,
            htr_page=deps.htr_page,
            extract_from_text=deps.extract_schulbericht,
        )
        if not sb:
            log.warning("Schulbericht HTR/Extract leer — Fallback E2E")
            sb = analyze_schulbericht_pdf(
                pdf_path,
                ocr_text,
                resolution=resolution,
                vision_page=deps.schulbericht_page_e2e,
            )
        meta = schulbericht_to_vision_meta(sb) if sb else {}
        if meta:
            meta["htr_profile"] = profile_name
            meta["_htr_pre_resolution"] = resolution.to_audit_dict()
        return meta

    htr = analyze_default_htr_pdf(
        pdf_path,
        resolution=resolution,
        htr_page=deps.htr_page,
    )
    if not (htr.get("volltext") or "").strip():
        log.warning("Default-HTR: leere Transkription")
        return {}
    meta = default_htr_to_vision_meta(htr)
    meta["htr_profile"] = profile_name
    meta["_htr_pre_resolution"] = resolution.to_audit_dict()
    return meta


HTR_CONTENT_MARKER = "--- Handschrift (HTR) ---"
HTR_CONTENT_EXCERPT_MAX = 1800
HTR_NOTE_FIELD_MAX = 600


def _truncate(text: str, limit: int) -> str:
    s = (text or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "…"


def extract_htr_searchable_text(meta: dict) -> str:
    """HTR-Ergebnis als Plain-Text für Paperless content (Volltextsuche)."""
    if not meta:
        return ""

    sb = meta.get("_schulbericht") or {}
    htr = meta.get("_htr") or sb.get("_htr") or {}

    parts: list[str] = []
    vor = (sb.get("schueler_vorname") or meta.get("schueler_vorname") or "").strip()
    nach = (sb.get("schueler_nachname") or meta.get("schueler_nachname") or "").strip()
    name = f"{vor} {nach}".strip() or (meta.get("empfaenger") or "").strip()
    if name:
        parts.append(f"Schüler: {name}")
    for key, label, src in [
        ("klasse", "Klasse", sb),
        ("semester_oder_zeitraum", "Zeitraum", sb),
        ("schule", "Schule", sb),
        ("lehrperson", "Lehrperson", sb),
    ]:
        val = (src.get(key) or meta.get(key) or "").strip()
        if val:
            parts.append(f"{label}: {val}")

    ah = (sb.get("arbeits_haltung") or sb.get("arbeitshaltung") or "").strip()
    leist = (sb.get("leistungen") or "").strip()
    if ah:
        parts.append(f"Arbeitshaltung: {ah}")
    if leist:
        parts.append(f"Leistungen: {leist}")

    if sb:
        hw_lines = clean_htr_lines([str(x) for x in (htr.get("handschrift_zeilen") or [])])
        if not hw_lines and htr:
            hw_lines = clean_htr_lines(rebuild_htr_volltext(htr).splitlines())
        if hw_lines:
            excerpt = _truncate("\n".join(hw_lines), HTR_CONTENT_EXCERPT_MAX)
            if parts:
                parts.append("")
            parts.append(excerpt)
        return "\n".join(parts).strip()

    voll = rebuild_htr_volltext(htr) if htr else ""
    if not voll:
        voll = (meta.get("htr_volltext") or meta.get("handschrift") or "").strip()
    if voll:
        if parts:
            parts.append("")
        parts.append(_truncate(voll, HTR_CONTENT_EXCERPT_MAX))
    elif not parts and meta.get("besonderheiten"):
        parts.append(str(meta["besonderheiten"]).strip())

    return "\n".join(parts).strip()


def build_htr_content_append(existing: str, htr_text: str) -> str:
    """HTR-Block an content anhängen (bei Re-Run alten Block ersetzen)."""
    existing = (existing or "").strip()
    htr_text = (htr_text or "").strip()
    if not htr_text:
        return existing
    idx = existing.find(HTR_CONTENT_MARKER)
    if idx >= 0:
        existing = existing[:idx].rstrip()
    block = f"{HTR_CONTENT_MARKER}\n{htr_text}"
    return f"{existing}\n\n{block}".strip() if existing else block


def format_htr_note_summary(meta: dict) -> str:
    """Kurzfassung für Paperless-Notiz — ohne Rohtranskript-Dump."""
    sb = meta.get("_schulbericht") or {}
    prof = meta.get("htr_profile") or ("schulbericht" if sb else "?")
    lines = [f"[paper.manager HTR — Profil: {prof}]"]

    conf = meta.get("schulbericht_confidence")
    if conf is None:
        conf = meta.get("htr_confidence")
    if conf is not None:
        lines.append(f"Confidence: {conf}")

    field_src = {**meta, **sb}
    for key, label in [
        ("schueler_vorname", "Vorname"),
        ("schueler_nachname", "Nachname"),
        ("klasse", "Klasse"),
        ("semester_oder_zeitraum", "Zeitraum"),
        ("schule", "Schule"),
        ("lehrperson", "Lehrperson"),
    ]:
        val = field_src.get(key)
        if val and str(val).strip():
            lines.append(f"{label}: {str(val).strip()}")

    ah = (sb.get("arbeits_haltung") or sb.get("arbeitshaltung") or "").strip()
    leist = (sb.get("leistungen") or "").strip()
    if ah:
        lines.append(f"Arbeitshaltung: {_truncate(ah, HTR_NOTE_FIELD_MAX)}")
    if leist:
        lines.append(f"Leistungen: {_truncate(leist, HTR_NOTE_FIELD_MAX)}")

    if sb:
        lines.append("Volltext-Auszug: Dokument-Inhalt (--- Handschrift ---)")
    elif meta.get("handschrift"):
        lines.append(f"Transkript: {_truncate(str(meta['handschrift']), HTR_NOTE_FIELD_MAX)}")

    return "\n".join(lines)
