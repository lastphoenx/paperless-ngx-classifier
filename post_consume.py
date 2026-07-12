#!/usr/bin/env python3
"""
post_consume_v12.68.py — Paperless-NGX Post-Consume Pipeline v12.68
Architektur:
  1. ocrmypdf         → bereits via pre_consume.sh erledigt
  2. Vision-LLM       → visuelle Metadaten + Layout-Signale (OLLAMA_MODEL_VISION)
  3. bge-m3           → Embeddings auf OCR-Text → Top-K ähnlichste Manifest-Einträge
  4. llama3.3:70b     → Entscheidung: Tags + Korrespondent + Storage Path
  5. Paperless API    → Metadaten setzen

Umgebungsvariablen (.env):
  PAPERLESS_URL           http://localhost:8000
  PAPERLESS_TOKEN         <token>           (gleich wie in v10)
  OLLAMA_BASE_URL         http://localhost:11434
  OLLAMA_MODEL_VISION     mistral-small3.1  (oder qwen2.5vl:32b)
  MANIFEST_PATH           /opt/paperless-scripts/training/manifest.json
  CORRECTIONS_PATH        /opt/paperless-scripts/training/corrections.jsonl
  LOG_PATH                /opt/paperless-scripts/logs/post_consume_v12.log
  RAG_TOP_K               5
  VISION_TIMEOUT          120
  LLM_TIMEOUT             180
  PAPERLESS_STORAGE_MODE  api  (api oder copy)
"""

import os

POST_CONSUME_VERSION = "12.74"  # 12.74: IBAN-Extraktion mit Modulo-97-Validierung (keine OCR-Falschtreffer)
import re
import sys
import json
import logging
import time
import base64
import subprocess
import tempfile
import traceback
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from brillenpass_parser import (
    REFRAKTION_JSON_SCHEMA,
    build_brillenpass_vision_prompt,
    corr_brillenpass_parsers,
    corr_supports_brillenpass,
    detect_parser,
    has_brillenpass_values,
    looks_like_brillenpass_any,
    looks_like_brillenpass_document,
    prefer_vision_for_brillenpass_merge,
    looks_like_optiker_rechnung,
    merge_brillenpass,
    parse_brillenpass_with_parsers,
    parse_by_parser,
    parse_fielmann_brillenpass,
    should_trigger_brillenpass,
    diagnose_brillenpass_extraction,
    snapshot_brillenpass,
)
from document_date import (
    DATUM_PROMPT_HINT,
    birth_dates_from_family,
    extract_document_issue_date,
    validate_issue_date,
)
from handwriting_vision import (
    HTR_ACTION_DEFER,
    HTR_ACTION_RUN,
    HtrPipelineDeps,
    audit_missed_correspondent_override,
    build_htr_content_append,
    decide_htr_action,
    extract_htr_searchable_text,
    is_schulbericht_htr_meta,
    normalize_document_type_key,
    run_htr_pipeline,
)
from schulbericht_vision import (
    EXTRACT_SYSTEM,
    HTR_NUM_PREDICT,
    HTR_VISION_SYSTEM,
    SCHULBERICHT_DPI,
    SCHULBERICHT_NUM_PREDICT,
    SCHULBERICHT_VISION_SYSTEM,
    analyze_schulbericht_pdf,
    analyze_schulbericht_two_stage,
    build_htr_transcribe_prompt,
    build_extract_from_transcript_prompt,
    build_schulbericht_vision_prompt,
    parse_htr_response,
    looks_like_schulbericht,
    schulbericht_to_vision_meta,
)

# ─── Konfiguration ────────────────────────────────────────────────────────────

PAPERLESS_URL     = os.environ.get("PAPERLESS_INTERNAL_URL", "http://localhost:8000")
PAPERLESS_TOKEN   = os.environ.get("PAPERLESS_TOKEN", "")           # gleich wie v10
OLLAMA_BASE       = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MANIFEST_PATH     = Path(os.environ.get("MANIFEST_PATH", "/opt/paperless-scripts/training/manifest.json"))
CORRECTIONS_PATH  = Path(os.environ.get("CORRECTIONS_PATH", "/opt/paperless-scripts/training/corrections.jsonl"))
ESCALATION_QUEUE  = Path(os.environ.get("ESCALATION_QUEUE", "/opt/paperless-scripts/training/escalation_queue.jsonl"))
DOCUMENT_REVIEW_QUEUE = Path(os.environ.get("DOCUMENT_REVIEW_QUEUE", "/opt/paperless-scripts/training/document_review_queue.jsonl"))
DOCUMENT_TYPES_JSON = Path(os.environ.get("DOCUMENT_TYPES_JSON", "/opt/paperless-scripts/training/document_types.json"))
TAGS_JSON           = Path(os.environ.get("TAGS_JSON",           "/opt/paperless-scripts/training/tags.json"))
FAMILY_JSON         = Path(os.environ.get("FAMILY_JSON",         "/opt/paperless-scripts/training/family.json"))

# ── Family-Config Cache ───────────────────────────────────────────────────────
_FAMILY_DATA:   dict | None = None

def _load_family() -> dict:
    """family.json laden — cached. Gibt leeres Dict bei Fehler."""
    global _FAMILY_DATA
    if _FAMILY_DATA is not None:
        return _FAMILY_DATA
    try:
        if FAMILY_JSON.exists():
            _FAMILY_DATA = json.loads(FAMILY_JSON.read_text(encoding="utf-8"))
            log.info("family.json geladen: %d Personen, %d Fahrzeuge, %d Beziehungen",
                     len(_FAMILY_DATA.get("personen", [])),
                     len(_FAMILY_DATA.get("fahrzeuge", [])),
                     len(_FAMILY_DATA.get("beziehungen", [])))
        else:
            _FAMILY_DATA = {}
    except Exception as e:
        log.warning("family.json laden fehlgeschlagen: %s", e)
        _FAMILY_DATA = {}
    return _FAMILY_DATA


def _get_haushalt_name() -> str:
    """Haushalts-Name aus family.json für LLM-Prompt."""
    return _load_family().get("haushalt", {}).get("name", "Haushalt")


def _fz_routing_ordner(fz: dict) -> bool:
    """Ob Kennzeichen-Match den Ziel-Ordner setzen soll (family.json).

    Explizit routing_ordner=false → nur CF/Person.
    Legacy: routing_ordner fehlt + ordner gesetzt → Routing an (Abwärtskompatibilität).
    """
    if "routing_ordner" in fz:
        return bool(fz.get("routing_ordner"))
    return bool((fz.get("ordner") or "").strip())


def _norm_kz_key(s: str) -> str:
    """Kennzeichen-Vergleichsschlüssel — nur A-Z/0-9, Gross (AG178626 = AG 178 626)."""
    import re as _re
    return _re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# Deterministisches Tag pro Fahrzeug aus family.json default_tag (muss in Paperless existieren)


def _build_kennzeichen_map() -> dict[str, dict]:
    """Kennzeichen → Fahrzeug-Eintrag aus family.json (CF, Person, optionales Ordner-Routing)."""
    result = {}
    for fz in _load_family().get("fahrzeuge", []):
        kz = _norm_kz_key(fz.get("kennzeichen", ""))
        if kz:
            result[kz] = {
                "ordner":              fz.get("ordner", ""),
                "person_id":           fz.get("person_id", ""),
                "kennzeichen_display": fz.get("kennzeichen", kz),
                "typ":                 (fz.get("typ") or "auto").lower(),
                "default_tag":         (fz.get("default_tag") or "").strip(),
                "routing_ordner":      _fz_routing_ordner(fz),
            }
    return result


def _build_beziehungen_map() -> list[dict]:
    """Beziehungen aus family.json — für Vision-Prompt Haushalt-Kontext."""
    return _load_family().get("beziehungen", [])


def _resolve_person_anzeigename(person_ref: str) -> str:
    """family.json person_id oder Anzeigename → Anzeigename für Select-CF «Person»."""
    if not person_ref or not str(person_ref).strip():
        return ""
    ref = str(person_ref).strip().lower()
    for p in _load_family().get("personen", []):
        pid = (p.get("id") or "").lower()
        name = (p.get("anzeigename") or "").strip()
        if pid and pid == ref:
            return name or p.get("id", "")
        if name and name.lower() == ref:
            return name
    return ""


def _resolve_person_id(person_ref: str) -> str:
    """family.json person_id oder Anzeigename → person_id."""
    if not person_ref or not str(person_ref).strip():
        return ""
    ref = str(person_ref).strip().lower()
    for p in _load_family().get("personen", []):
        pid = (p.get("id") or "").strip()
        name = (p.get("anzeigename") or "").strip()
        if pid and pid.lower() == ref:
            return pid
        if name and name.lower() == ref:
            return pid
    return str(person_ref).strip()


_AHV_OCR_RE = re.compile(r"756[\s.]?\d{4}[\s.]?\d{4}[\s.]?\d{2}")


def _norm_ahv_digits(ahv: str) -> str:
    digits = re.sub(r"\D", "", (ahv or "").strip())
    return digits if len(digits) == 13 and digits.startswith("756") else ""


def _extract_ahvs_from_text(text: str) -> set[str]:
    found: set[str] = set()
    for m in _AHV_OCR_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", m.group())
        if len(digits) == 13 and digits.startswith("756"):
            found.add(digits)
    return found


def _parse_geburtsdatum(geb: str) -> tuple[int, int, int] | None:
    """Parst Geburtsdatum: 15.5.1980, 15.05.1980, 15.5.80, 1980-05-15."""
    geb = (geb or "").strip()
    if not geb:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", geb)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return d, mo, y
        return None
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$", geb)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y = 2000 + y if y < 30 else 1900 + y
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return d, mo, y
    return None


def _normalize_geburtsdatum(geb: str) -> str:
    """Speicherformat: 15.5.1980 (ohne führende Nullen)."""
    parsed = _parse_geburtsdatum(geb)
    if not parsed:
        return geb.strip()
    d, mo, y = parsed
    return f"{d}.{mo}.{y}"


def _geburtsdatum_search_patterns(geb: str) -> list[str]:
    """Suchmuster für Geburtsdatum im OCR-Text — CH/DE-Üblichkeiten."""
    geb = (geb or "").strip()
    parsed = _parse_geburtsdatum(geb)
    if not parsed:
        return [geb] if geb else []
    d, mo, y = parsed
    y2 = y % 100
    patterns = [
        f"{d}.{mo}.{y}",
        f"{d:02d}.{mo:02d}.{y}",
        f"{d}.{mo}.{y2}",
        f"{d:02d}.{mo:02d}.{y2:02d}",
        f"{y}-{mo:02d}-{d:02d}",
        f"{d}/{mo}/{y}",
        f"{d:02d}/{mo:02d}/{y}",
    ]
    if geb not in patterns:
        patterns.insert(0, geb)
    return list(dict.fromkeys(patterns))


def _person_name_candidates(p: dict) -> list[str]:
    """Anzeigename + Varianten für Textsuche (min. 4 Zeichen)."""
    names = []
    for key in ("anzeigename",):
        v = (p.get(key) or "").strip()
        if len(v) >= 4:
            names.append(v)
    for v in p.get("namen_varianten") or []:
        v = str(v).strip()
        if len(v) >= 4:
            names.append(v)
    return names


def _match_person_direct(ocr_text: str, vision_meta: dict | None = None) -> tuple[str, str]:
    """Direkte Person-Zuweisung ohne Korrespondent/Fahrzeug — AHV > Geb.datum > Name."""
    if not CF_PERSON:
        return "", ""
    personen = _load_family().get("personen", [])
    if not personen:
        return "", ""

    parts = [ocr_text or ""]
    if vision_meta:
        for key in ("empfaenger", "text", "inhalt", "adressat"):
            val = vision_meta.get(key)
            if val:
                parts.append(str(val))
    text = "\n".join(parts)

    # 1. AHV — eindeutig
    found_ahvs = _extract_ahvs_from_text(text)
    if found_ahvs:
        ahv_hits = []
        for p in personen:
            norm = _norm_ahv_digits(p.get("ahv_nummer", ""))
            if norm and norm in found_ahvs:
                ahv_hits.append(p)
        if len(ahv_hits) == 1:
            name = (ahv_hits[0].get("anzeigename") or "").strip()
            if name:
                return name, "AHV"

    # 2. Geburtsdatum — nur bei eindeutigem Treffer
    geb_hits = []
    for p in personen:
        geb = (p.get("geburtsdatum") or "").strip()
        if not geb:
            continue
        if any(pat in text for pat in _geburtsdatum_search_patterns(geb)):
            geb_hits.append(p)
    if len(geb_hits) == 1:
        name = (geb_hits[0].get("anzeigename") or "").strip()
        if name:
            return name, "Geburtsdatum"

    # 3. Name/Varianten — nur bei eindeutigem Treffer (Wortgrenze, ≥4 Zeichen)
    name_hits = []
    for p in personen:
        for cand in _person_name_candidates(p):
            pattern = re.compile(r"(?<!\w)" + re.escape(cand) + r"(?!\w)", re.IGNORECASE)
            if pattern.search(text):
                name_hits.append(p)
                break
    if len(name_hits) == 1:
        name = (name_hits[0].get("anzeigename") or "").strip()
        if name:
            return name, "Name"

    return "", ""


def _get_haushalt_personen_namen() -> list[str]:
    """Alle Personennamen im Haushalt — für Vision-Prompt (sind NIE Absender)."""
    namen = []
    for p in _load_family().get("personen", []):
        if p.get("anzeigename"):
            namen.append(p["anzeigename"])
    return namen


def _corr_kandidaten_strings(entry: dict) -> list[str]:
    """Alle Suchstrings eines Korrespondenten (Name, Varianten, Match)."""
    return (
        [entry.get("name", "").lower()] +
        [v.lower() for v in entry.get("varianten", [])] +
        [m.lower() for m in entry.get("match", [])]
    )


def _is_corr_platzhalter(entry: dict) -> bool:
    """Platzhalter-Korrespondenten (kein echter Absender) nicht automatisch zuordnen."""
    return bool(entry.get("platzhalter"))


def _resolve_corr_entry(corr_map: dict, absender: str) -> dict | None:
    """Korrespondent zu Absender/LLM-Name (Stufe 1 + resolve_correspondent).

    Reihenfolge:
      1. Exakter Match auf match[] (normalisiert)
      2. Substring über alle Einträge — längster Treffer gewinnt
      3. Token-Overlap ≥2 nur mit mindestens einem Wort ≥4 Zeichen
    """
    if not absender or not corr_map:
        return None
    absender_lower = absender.lower().strip()
    abs_norm = _normalize_corr(absender)

    for entry in corr_map.get("eintraege", []):
        if _is_corr_platzhalter(entry):
            continue
        for m in entry.get("match", []):
            if _normalize_corr(m) == abs_norm:
                return entry

    best_entry, best_len = None, 0
    for entry in corr_map.get("eintraege", []):
        if _is_corr_platzhalter(entry):
            continue
        for k in _corr_kandidaten_strings(entry):
            if not k or len(k) < 3:
                continue
            if k in absender_lower or absender_lower in k:
                if len(k) > best_len:
                    best_len, best_entry = len(k), entry
    if best_entry:
        return best_entry

    for entry in corr_map.get("eintraege", []):
        if _is_corr_platzhalter(entry):
            continue
        for k in _corr_kandidaten_strings(entry):
            if not k or len(k) < 3:
                continue
            k_words   = {w for w in k.split() if w not in _TOKEN_OVERLAP_STOPWORDS}
            abs_words = {w for w in absender_lower.split() if w not in _TOKEN_OVERLAP_STOPWORDS}
            overlap = k_words & abs_words
            if len(overlap) >= 2 and any(len(w) >= 4 for w in overlap):
                return entry
    return None


def _match_korrespondent_eintrag(corr_map: dict, absender: str) -> dict | None:
    """Alias — identisch mit _resolve_corr_entry."""
    return _resolve_corr_entry(corr_map, absender)


def _norm_corr_uid(raw: str) -> str:
    """CHE-106.827.671 MWST → CHE106827671 (Vergleich)."""
    if not raw:
        return ""
    s = re.sub(r"[^A-Za-z0-9]", "", str(raw).upper())
    if s.startswith("CHE") and len(s) >= 12:
        return s[:12]
    return s


def _norm_corr_iban(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw).upper())


def _norm_corr_telefon(raw: str) -> str:
    """Schweizer Telefonnummer → Ziffernfolge (41…)."""
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("41") and len(digits) >= 11:
        return digits
    if digits.startswith("0") and len(digits) >= 10:
        return "41" + digits[1:]
    return digits


def _norm_corr_email(raw: str) -> str:
    return str(raw or "").strip().lower()


def _household_emails() -> set[str]:
    """Empfänger-E-Mails aus family.json — nicht als Korrespondent-Identifikator."""
    out: set[str] = set()
    for p in _load_family().get("personen", []):
        for key in ("email", "emails"):
            val = p.get(key)
            if isinstance(val, list):
                for item in val:
                    n = _norm_corr_email(item)
                    if n:
                        out.add(n)
            elif val:
                n = _norm_corr_email(str(val))
                if n:
                    out.add(n)
    return out


_CORR_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)
_CORR_EMAIL_VON_RE = re.compile(
    r'(?:Von|From|Absender|E-?Mail)\s*[:\s"]*[^<\n"]*<([^>@\s]+@[^>\s]+)>',
    re.IGNORECASE,
)
_CORR_EMAIL_VON_PLAIN_RE = re.compile(
    r'(?:Von|From|Absender)\s*[:\s]*["\']?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)
_CORR_EMAIL_AN_RE = re.compile(
    r'(?:An|To|Empfänger|Empfaenger|Recipient)\s*[:\s"]*[^<\n"]*<([^>@\s]+@[^>\s]+)>',
    re.IGNORECASE,
)
_CORR_EMAIL_AN_PLAIN_RE = re.compile(
    r'(?:An|To|Empfänger|Empfaenger)\s*[:\s]*["\']?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)
_CORR_TEL_CH_RE = re.compile(
    r"(?:\+41|0041)\s*[\-]?\s*(?:\d{2}|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{2}[\s.\-]?\d{2}\b",
)
_CORR_UID_RE = re.compile(
    r"CHE[-\s.]?\d{3}[-\s.]?\d{3}[-\s.]?\d{3}(?:\s*MWST)?",
    re.IGNORECASE,
)
from iban_utils import extract_ibans_from_text, fix_iban_ocr_compact, format_iban_display, validate_iban
    ocr_text: str,
    qr_meta: dict | None = None,
    vision_meta: dict | None = None,
) -> str:
    parts = [ocr_text or ""]
    if qr_meta:
        for key in ("iban", "referenz", "zusatzinfo"):
            val = qr_meta.get(key)
            if val:
                parts.append(str(val))
    if vision_meta:
        for key in ("absender", "text", "inhalt", "empfaenger"):
            val = vision_meta.get(key)
            if val:
                parts.append(str(val))
    return "\n".join(parts)


def _extract_corr_uids_from_text(text: str) -> set[str]:
    found: set[str] = set()
    for m in _CORR_UID_RE.findall(text or ""):
        n = _norm_corr_uid(m)
        if n:
            found.add(n)
    return found


def _extract_corr_ibans_from_text(text: str) -> set[str]:
    found: set[str] = set()
    for display in extract_ibans_from_text(text, max_results=10):
        valid = validate_iban(display)
        if valid:
            found.add(valid)
    return found


def _recipient_emails_from_text(text: str) -> set[str]:
    """Empfänger aus An/To-Zeilen — nicht als Korrespondent-Identifikator."""
    out: set[str] = set()
    for pat in (_CORR_EMAIL_AN_RE, _CORR_EMAIL_AN_PLAIN_RE):
        for m in pat.findall(text or ""):
            n = _norm_corr_email(m)
            if n:
                out.add(n)
    return out


def _is_ignored_email(email_norm: str, *, extra_ignore: set[str] | None = None) -> bool:
    if not email_norm:
        return True
    if email_norm in _household_emails():
        return True
    if extra_ignore and email_norm in extra_ignore:
        return True
    return any(x in email_norm for x in ("noreply", "no-reply", "donotreply", "mailer-daemon"))


def _extract_corr_emails_from_text(text: str) -> list[str]:
    """E-Mails aus OCR — Von/From zuerst, Empfänger/Haushalt ausfiltern."""
    found: list[str] = []
    seen: set[str] = set()
    recipients = _recipient_emails_from_text(text)

    def _add(raw: str) -> None:
        n = _norm_corr_email(raw)
        if not n or n in seen or _is_ignored_email(n, extra_ignore=recipients):
            return
        seen.add(n)
        found.append(raw.strip())

    for pat in (_CORR_EMAIL_VON_RE, _CORR_EMAIL_VON_PLAIN_RE):
        for m in pat.findall(text or ""):
            _add(m)
    for m in _CORR_EMAIL_RE.findall(text or ""):
        _add(m)
    return found


def _corr_phone_in_text(phone_norm: str, digit_stream: str) -> bool:
    if not phone_norm or len(phone_norm) < 9:
        return False
    return phone_norm in digit_stream


def _match_correspondent_by_identifikatoren(
    corr_map: dict,
    ocr_text: str,
    *,
    qr_meta: dict | None = None,
    vision_meta: dict | None = None,
) -> tuple[dict | None, str]:
    """Deterministischer Korrespondent-Match: UID > IBAN > E-Mail > Telefon (nur eindeutig)."""
    text = _corr_document_search_text(ocr_text, qr_meta, vision_meta)
    if not text.strip():
        return None, ""
    doc_uids = _extract_corr_uids_from_text(text)
    doc_ibans = _extract_corr_ibans_from_text(text)
    doc_emails = { _norm_corr_email(e) for e in _extract_corr_emails_from_text(text) }
    digit_stream = re.sub(r"\D", "", text)

    uid_hits: list[dict] = []
    iban_hits: list[dict] = []
    email_hits: list[dict] = []
    tel_hits: list[dict] = []

    for entry in corr_map.get("eintraege", []):
        if _is_corr_platzhalter(entry):
            continue
        ident = entry.get("identifikatoren") or {}
        for uid in ident.get("uid", []) or []:
            if _norm_corr_uid(uid) in doc_uids:
                uid_hits.append(entry)
                break
        for iban in ident.get("iban", []) or []:
            if _norm_corr_iban(iban) in doc_ibans:
                iban_hits.append(entry)
                break
        for em in ident.get("email", []) or []:
            if _norm_corr_email(em) in doc_emails:
                email_hits.append(entry)
                break
        for tel in ident.get("telefon", []) or []:
            if _corr_phone_in_text(_norm_corr_telefon(tel), digit_stream):
                tel_hits.append(entry)
                break

    if len(uid_hits) == 1:
        return uid_hits[0], "UID"
    if len(uid_hits) > 1:
        log.warning("Identifikator UID: mehrdeutig (%d Treffer)", len(uid_hits))
        return None, ""

    if len(iban_hits) == 1:
        return iban_hits[0], "IBAN"
    if len(iban_hits) > 1:
        log.warning("Identifikator IBAN: mehrdeutig (%d Treffer)", len(iban_hits))
        return None, ""

    if len(email_hits) == 1:
        return email_hits[0], "E-Mail"
    if len(email_hits) > 1:
        log.warning("Identifikator E-Mail: mehrdeutig (%d Treffer)", len(email_hits))
        return None, ""

    if len(tel_hits) == 1:
        return tel_hits[0], "Telefon"
    if len(tel_hits) > 1:
        log.warning("Identifikator Telefon: mehrdeutig (%d Treffer)", len(tel_hits))
        return None, ""

    return None, ""


def _format_iban_display(compact: str) -> str:
    return format_iban_display(compact)


def _fix_iban_ocr_compact(compact: str) -> str:
    return fix_iban_ocr_compact(compact)


def _extract_identifikatoren_vorschlag(
    ocr_text: str,
    qr_meta: dict | None = None,
    vision_meta: dict | None = None,
) -> dict:
    """UID/IBAN/E-Mail/Telefon aus Dokument für Korrespondenten-Review-Vorschlag."""
    text = _corr_document_search_text(ocr_text, qr_meta, vision_meta)
    uid_out: list[str] = []
    iban_out: list[str] = []
    email_out: list[str] = []
    tel_out: list[str] = []
    tel_seen: set[str] = set()
    iban_seen: set[str] = set()
    email_seen: set[str] = set()

    for m in _CORR_UID_RE.findall(text):
        s = m.strip().rstrip(".")
        if s and s not in uid_out:
            uid_out.append(s)

    if qr_meta and qr_meta.get("iban"):
        valid = validate_iban(qr_meta["iban"])
        if valid and valid not in iban_seen:
            iban_seen.add(valid)
            iban_out.append(_format_iban_display(valid))

    for display in extract_ibans_from_text(text, max_results=3):
        compact = _norm_corr_iban(display)
        if compact and compact not in iban_seen:
            iban_seen.add(compact)
            iban_out.append(display)

    for em in _extract_corr_emails_from_text(text):
        n = _norm_corr_email(em)
        if n and n not in email_seen:
            email_seen.add(n)
            email_out.append(n)

    _tel_label_re = re.compile(
        r"(?:Telefon|Tel\.?|Fax|Telefax)\s*[:\s]*([+()0-9][\d\s./\-]{7,28})",
        re.IGNORECASE,
    )
    for m in _tel_label_re.findall(text):
        t = re.sub(r"\s+", " ", m.strip().rstrip(" -"))
        n = _norm_corr_telefon(t)
        if n and n not in tel_seen and len(n) >= 9:
            tel_seen.add(n)
            tel_out.append(t)

    for m in _CORR_TEL_CH_RE.findall(text):
        t = re.sub(r"\s+", " ", m.strip())
        n = _norm_corr_telefon(t)
        if n and n not in tel_seen and len(n) >= 9:
            tel_seen.add(n)
            tel_out.append(t)

    return {
        "uid": uid_out[:3],
        "iban": iban_out[:2],
        "email": email_out[:3],
        "telefon": tel_out[:3],
    }


def _doctyp_matches_visuell(visuell: str, erlaubte_doctypen: list[str]) -> bool:
    """Prüft ob dokumenttyp_visuell (Vision-Freitext) einem der erlaubten Doctypen entspricht.

    Löst beide Seiten über _SYNONYM_MAP auf → kanonischen Namen → Vergleich.
    Damit matcht "Gehaltsabrechnung" gegen erlaubte=["Lohnabrechnung"] wenn beide
    auf den gleichen kanonischen Typ zeigen.
    """
    if not visuell or not erlaubte_doctypen:
        return False
    _load_known_doctypes()

    def _kanonisch(name: str) -> str:
        """Gibt kanonischen Typ-Namen zurück (über Direktname oder Synonym)."""
        n = name.lower().strip()
        if n in _KNOWN_DOCTYPE_CACHE:
            return n
        return _SYNONYM_MAP.get(n, n)  # Synonym → kanonisch, sonst original

    visuell_kan = _kanonisch(visuell)
    for erlaubt in erlaubte_doctypen:
        if _kanonisch(erlaubt) == visuell_kan:
            return True
    return False


def _norm_ref(s: str) -> str:
    import re as _re
    return _re.sub(r"[\s_\-\.]", "", (s or "").lower())


def _vision_ref_values(vision_meta: dict | None) -> list[str]:
    """Referenz-Kandidaten aus Vision (Policen-/Kunden-/Rechnungsnummer)."""
    if not vision_meta:
        return []
    out: list[str] = []
    for key in ("policennummer", "kundennummer", "rechnungsnummer"):
        v = str(vision_meta.get(key) or "").strip()
        if v and v.lower() not in ("null", "none", ""):
            out.append(v.lower())
    return out


def _referenznummer_im_dokument(
    ref: str,
    ocr_lower: str,
    extrahierte_werte: set[str],
    vision_refs: list[str],
) -> bool:
    """Prüft ob referenznummer im OCR, per Regex oder in Vision-Feldern vorkommt."""
    ref_lower = ref.strip().lower()
    if not ref_lower:
        return False
    if ref_lower in ocr_lower:
        return True
    ref_norm = _norm_ref(ref_lower)
    for val in extrahierte_werte:
        if _norm_ref(val) == ref_norm:
            return True
    for v in vision_refs:
        if _norm_ref(v) == ref_norm:
            return True
    return False


def _beziehung_match_korpus(ocr_lower: str, vision_meta: dict | None) -> str:
    """Suchtext für Stichwort-Tiebreaker: OCR + ausgewählte Vision-Felder."""
    parts = [ocr_lower]
    if vision_meta:
        for key in ("dokumenttyp_visuell", "besonderheiten", "layout"):
            v = str(vision_meta.get(key) or "").strip().lower()
            if v and v not in ("null", "none", ""):
                parts.append(v)
    return " ".join(parts)


def _beziehung_stichwort_treffer(bez: dict, korpus: str) -> bool:
    """True wenn mindestens ein Stichwort der Beziehung im Korpus vorkommt."""
    for sw in bez.get("stichworte") or []:
        s = str(sw).strip().lower()
        if s and s in korpus:
            return True
    return False


def _tiebreak_ref_matches(
    ref_matches: list[dict],
    dokumenttyp_visuell: str,
    ocr_lower: str,
    vision_meta: dict | None,
) -> dict | None:
    """Ein eindeutiger Treffer bei mehreren Ref-Matches, sonst None."""
    n = len(ref_matches)

    # 1. Stichworte (nur Beziehungen mit gepflegten Stichworten)
    korpus = _beziehung_match_korpus(ocr_lower, vision_meta)
    sw_matches = [bez for bez in ref_matches if _beziehung_stichwort_treffer(bez, korpus)]
    if len(sw_matches) == 1:
        b = sw_matches[0]
        log.info(
            "Beziehungs-Match: %d Ref-Matches → Tiebreaker Stichworte → person=%s ordner=%s",
            n, b.get("person"), b.get("ordner"),
        )
        return b
    if len(sw_matches) > 1:
        log.warning(
            "Beziehungs-Match: Stichwort-Tiebreaker uneindeutig (%d Treffer) → dokumenttyp_visuell",
            len(sw_matches),
        )

    # 2. dokumenttyp_visuell gegen erlaubte_doctypen (Synonym-aware)
    if dokumenttyp_visuell:
        dt_matches = [
            bez for bez in ref_matches
            if bez.get("erlaubte_doctypen")
            and _doctyp_matches_visuell(dokumenttyp_visuell, bez["erlaubte_doctypen"])
        ]
        if len(dt_matches) == 1:
            log.info(
                "Beziehungs-Match: %d Ref-Matches → Tiebreaker via dokumenttyp_visuell='%s' → person=%s",
                n, dokumenttyp_visuell, dt_matches[0].get("person"),
            )
            return dt_matches[0]
        if len(dt_matches) > 1:
            log.warning(
                "Beziehungs-Match: Tiebreaker uneindeutig (%d Matches für '%s') → LLM",
                len(dt_matches), dokumenttyp_visuell,
            )
        else:
            log.warning(
                "Beziehungs-Match: Tiebreaker kein Treffer für '%s' (erlaubte_doctypen/Synonym) → LLM",
                dokumenttyp_visuell,
            )
    else:
        log.warning("Beziehungs-Match: %d Referenznummer-Matches — nicht eindeutig → LLM", n)
    return None


def _match_beziehung_v2(
    corr_entry: dict,
    vision_empfaenger: str,
    ocr_text: str,
    dokumenttyp_visuell: str = "",
    vision_meta: dict | None = None,
) -> dict | None:
    """Stufe 1: Beziehungs-Match auf Korrespondenten-Eintrag.

    Match-Reihenfolge (stärkster zuerst):
      1. referenznummer in OCR, extraktion_muster oder Vision (Police/Kunde/Rechnung)
         1a. Mehrere Ref-Treffer: Stichworte (OCR/Vision) → dokumenttyp_visuell
         1b. Tiebreaker: dokumenttyp_visuell gegen erlaubte_doctypen (Synonym-aware)
      2. nur eine Beziehung **ohne** referenznummer → deterministisch
      3. empfaenger (Vision) stimmt mit person überein — nur Beziehungen ohne referenznummer

    Hat eine Beziehung eine referenznummer, gilt sie nur bei Ref-Match (kein Einzel-/Empfänger-Fallback).
    """
    import re as _re
    beziehungen = corr_entry.get("beziehungen", [])
    if not beziehungen:
        return None

    personen_map = {
        p.get("id", "").lower(): p.get("anzeigename", "")
        for p in _load_family().get("personen", [])
    }
    ocr_lower = ocr_text.lower()
    vision_refs = _vision_ref_values(vision_meta)

    # Extraktions-Muster des Korrespondenten vorab auswerten → extrahierte Werte
    extrahierte_werte: set[str] = set()
    for muster_key, muster in corr_entry.get("extraktion_muster", {}).items():
        regex = muster.get("regex", "")
        if not regex:
            continue
        try:
            for m in _re.finditer(regex, ocr_text, _re.IGNORECASE):
                # Named group oder ganzer Match
                val = next(iter(m.groupdict().values()), None) or m.group(0)
                if val:
                    extrahierte_werte.add(val.strip().lower())
        except Exception:
            pass

    # Match 1: Referenznummer — OCR, Regex-Extraktion oder Vision-Felder
    ref_matches = []
    for bez in beziehungen:
        ref = (bez.get("referenznummer") or "").strip()
        if not ref:
            continue
        if _referenznummer_im_dokument(ref, ocr_lower, extrahierte_werte, vision_refs):
            ref_matches.append(bez)

    if len(ref_matches) == 1:
        log.info("Beziehungs-Match via Referenznummer → person=%s", ref_matches[0].get("person"))
        return ref_matches[0]
    elif len(ref_matches) > 1:
        picked = _tiebreak_ref_matches(
            ref_matches, dokumenttyp_visuell, ocr_lower, vision_meta,
        )
        if picked:
            return picked
        return None

    # Match 2: Einzige Beziehung ohne Referenznummer → deterministisch
    if len(beziehungen) == 1:
        sole = beziehungen[0]
        if not (sole.get("referenznummer") or "").strip():
            log.info("Beziehungs-Match: einzige Beziehung (ohne Ref-Nr) → person=%s", sole.get("person"))
            return sole

    # Match 3: Empfänger aus Vision — nur Beziehungen ohne referenznummer, genau 1 Match
    if vision_empfaenger:
        empf_lower = vision_empfaenger.lower().strip()
        empf_matches = []
        for bez in beziehungen:
            if (bez.get("referenznummer") or "").strip():
                continue
            person_id   = bez.get("person", "").lower()
            anzeigename = personen_map.get(person_id, person_id).lower()
            if person_id in empf_lower or anzeigename in empf_lower:
                empf_matches.append(bez)
        if len(empf_matches) == 1:
            log.info("Beziehungs-Match via Empfänger '%s' → person=%s",
                     vision_empfaenger, empf_matches[0].get("person"))
            return empf_matches[0]
        elif len(empf_matches) > 1:
            log.warning("Beziehungs-Match: %d Empfänger-Matches — nicht eindeutig → LLM", len(empf_matches))

    return None


def _match_beziehung(absender: str) -> dict | None:
    """Legacy: Beziehungs-Match aus family.json (für Vision-Prompt-Kontext).
    Hauptrouting läuft jetzt via correspondents.json (_match_beziehung_v2).
    """
    if not absender:
        return None
    absender_lower = absender.lower().strip()
    for bez in _build_beziehungen_map():
        korr = bez.get("korrespondent", "").lower().strip()
        if not korr:
            continue
        if korr in absender_lower or absender_lower in korr:
            return bez
        korr_words = {w for w in korr.split() if w not in _TOKEN_OVERLAP_STOPWORDS}
        abs_words  = {w for w in absender_lower.split() if w not in _TOKEN_OVERLAP_STOPWORDS}
        if korr_words and abs_words and len(korr_words & abs_words) >= 2:
            return bez
    return None

# ── Tag-Ausschluss-Cache ──────────────────────────────────────────────────────
_DT_FIX_TAGS_MAP:    dict[str, list[str]] = {}
_DT_FELDPROFIL_MAP:  dict[str, dict] = {}
_DT_FIX_TAGS_LOADED: bool = False

def _load_dt_fix_tags_map() -> None:
    """Dokumenttyp → fix_tags + feldprofil aus document_types.json.
    In-Process-Singleton — konsistent mit Korrespondenten-Cache und Tag-Ausschluss-Cache.
    post_consume.py wird pro Scan neu gestartet → kein manueller Reload nötig.
    Änderungen in document_types.json wirken beim nächsten Scan automatisch.
    """
    global _DT_FIX_TAGS_LOADED
    if _DT_FIX_TAGS_LOADED:
        return
    try:
        if DOCUMENT_TYPES_JSON.exists():
            data = json.loads(DOCUMENT_TYPES_JSON.read_text(encoding="utf-8"))
            for t in data.get("typen", []):
                key = t["name"].lower()
                tags = t.get("fix_tags", [])
                if tags:
                    _DT_FIX_TAGS_MAP[key] = tags
                profil = t.get("feldprofil", {})
                if profil:
                    _DT_FELDPROFIL_MAP[key] = profil
        _DT_FIX_TAGS_LOADED = True
        if _DT_FIX_TAGS_MAP:
            log.info("DocType fix_tags Map: %d Typen geladen", len(_DT_FIX_TAGS_MAP))
        if _DT_FELDPROFIL_MAP:
            log.info("DocType feldprofil Map: %d Typen geladen", len(_DT_FELDPROFIL_MAP))
    except Exception as e:
        log.warning("DocType fix_tags/feldprofil Map laden fehlgeschlagen: %s", e)

def _get_doctype_fix_tags(doctyp_name: str) -> list[str]:
    """fix_tags für einen Dokumenttyp zurückgeben."""
    _load_dt_fix_tags_map()
    return _DT_FIX_TAGS_MAP.get((doctyp_name or "").lower(), [])

def _get_feldprofil_for_doctype(doctyp_name: str) -> dict:
    """feldprofil für einen Dokumenttyp zurückgeben (leer = alle Felder erlaubt)."""
    _load_dt_fix_tags_map()
    return _DT_FELDPROFIL_MAP.get((doctyp_name or "").lower(), {})
_TAG_AUSSCHLUSS_MAP:    dict[str, list[str]] = {}
_TAG_AUSSCHLUSS_LOADED: bool = False


def _load_tag_ausschluss_map() -> None:
    global _TAG_AUSSCHLUSS_LOADED
    if _TAG_AUSSCHLUSS_LOADED:
        return
    try:
        if TAGS_JSON.exists():
            data = json.loads(TAGS_JSON.read_text(encoding="utf-8"))
            for t in data.get("tags", []):
                kws = [k.lower() for k in t.get("ausschliessen", []) if k]
                if kws:
                    _TAG_AUSSCHLUSS_MAP[t["name"].lower()] = kws
        _TAG_AUSSCHLUSS_LOADED = True
        if _TAG_AUSSCHLUSS_MAP:
            log.info("Tag-Ausschluss-Map: %d Tags mit Keywords geladen", len(_TAG_AUSSCHLUSS_MAP))
    except Exception as e:
        log.warning("Tag-Ausschluss-Map laden fehlgeschlagen: %s", e)


def _filter_excluded_tags(tags: list[str], ocr_text: str, vision_meta: dict) -> list[str]:
    """Entfernt Tags deren Ausschluss-Keywords im Dokument vorkommen."""
    _load_tag_ausschluss_map()
    if not _TAG_AUSSCHLUSS_MAP:
        return tags
    search_text = " ".join(filter(None, [
        ocr_text[:2000] if ocr_text else "",
        str(vision_meta.get("absender") or "") if vision_meta else "",
        str(vision_meta.get("dokumenttyp_visuell") or "") if vision_meta else "",
    ])).lower()
    filtered, removed = [], []
    for tag in tags:
        kws = _TAG_AUSSCHLUSS_MAP.get(tag.lower(), [])
        hit = next((k for k in kws if k in search_text), None)
        if hit:
            removed.append(f"{tag}('{hit}')")
        else:
            filtered.append(tag)
    if removed:
        log.info("Tag-Ausschluss: entfernt %s", ", ".join(removed))
    return filtered

# ── Custom Field IDs (in Paperless angelegt) ──────────────────────────────────
# Überschreibbar via .env falls IDs sich ändern
CF_BETRAG          = int(os.environ.get("CF_BETRAG_ID",          "1"))
CF_RECHNUNGSNUMMER = int(os.environ.get("CF_RECHNUNGSNUMMER_ID", "5"))
CF_KUNDENNUMMER    = int(os.environ.get("CF_KUNDENNUMMER_ID",    "6"))
CF_QR_REFERENZ     = int(os.environ.get("CF_QR_REFERENZ_ID",     "7"))
CF_FAELLIG_AM      = int(os.environ.get("CF_FAELLIG_AM_ID",      "8"))
CF_STATUS          = int(os.environ.get("CF_STATUS_ID",          "9"))
CF_POLICENNUMMER   = int(os.environ.get("CF_POLICENNUMMER_ID",   "10"))
CF_KENNZEICHEN     = int(os.environ.get("CF_KENNZEICHEN_ID",     "11"))
CF_BEZAHLT_AM      = int(os.environ.get("CF_BEZAHLT_AM_ID",      "12"))
CF_GESCANNT_AM     = int(os.environ.get("CF_GESCANNT_AM_ID",     "13"))
CF_VERARBEITUNG    = int(os.environ.get("CF_VERARBEITUNG_ID",    "0"))  # 0 = deaktiviert
CF_PERSON          = int(os.environ.get("CF_PERSON_ID",          "0"))  # 0 = deaktiviert
CF_DOK_ID          = int(os.environ.get("CF_DOK_ID",             "0"))  # 0 = deaktiviert — Paperless-Dokument-ID
CF_STEUERJAHR      = int(os.environ.get("CF_STEUERJAHR_ID",        "0"))  # 0 = deaktiviert — Integer
STEUERRELEVANT_TAG = os.environ.get("STEUERRELEVANT_TAG", "Steuerrelevant")

# Tags die diesen Regex-Mustern entsprechen lösen KEINEN Confidence-Downgrade aus
# wenn sie verworfen werden (z.B. Jahreszahlen, Monat.Jahr).
# Komma-getrennte Python-Regex in .env: CONFIDENCE_IGNORE_TAG_PATTERNS=^\d{4}$,^\d{1,2}\.\d{4}$
import re as _re
_raw_ignore_patterns = os.environ.get("CONFIDENCE_IGNORE_TAG_PATTERNS", r"^\d{4}$,^\d{1,2}\.\d{4}$")
CONFIDENCE_IGNORE_TAG_PATTERNS = [
    _re.compile(p.strip()) for p in _raw_ignore_patterns.split(",") if p.strip()
]

def _is_trivial_tag_violation(tag: str) -> bool:
    """True wenn der Tag trivial ist und keinen Confidence-Downgrade rechtfertigt."""
    return any(p.match(tag) for p in CONFIDENCE_IGNORE_TAG_PATTERNS)
LOG_PATH          = Path(os.environ.get("LOG_PATH", "/opt/paperless-scripts/logs/post_consume_v12.log"))
RAG_TOP_K         = int(os.environ.get("RAG_TOP_K", "5"))
VISION_TIMEOUT    = int(os.environ.get("VISION_TIMEOUT", "120"))
LLM_TIMEOUT       = int(os.environ.get("LLM_TIMEOUT", "300"))
STORAGE_MODE      = os.environ.get("PAPERLESS_STORAGE_MODE", "api").lower()

# Samples: ausgebaut (v12.8) — kein Trainings-Loop vorhanden, Timing-Bug bei Paperless-Verschiebung

MODEL_VISION = os.environ.get("OLLAMA_MODEL_VISION", "qwen2.5vl:7b")
MODEL_EMBED  = "bge-m3"
MODEL_LLM    = os.environ.get("OLLAMA_MODEL_LLM", "llama3.3:70b")

BRILLENPASS_VISION_FALLBACK = os.environ.get("BRILLENPASS_VISION_FALLBACK", "0").strip().lower() in (
    "1", "true", "yes",
)
BRILLENPASS_TESSERACT = os.environ.get("BRILLENPASS_TESSERACT", "1").strip().lower() not in (
    "0", "false", "no",
)
BRILLENPASS_VISION_ON_GAPS = os.environ.get("BRILLENPASS_VISION_ON_GAPS", "1").strip().lower() not in (
    "0", "false", "no",
)
BRILLENPASS_MIN_HEADER_ANCHORS = int(
    os.environ.get("BRILLENPASS_MIN_HEADER_ANCHORS", "3").split("#")[0].strip() or "3"
)

MEDIA_ROOT = os.environ.get(
    "PAPERLESS_MEDIA_ROOT",
    os.environ.get("MEDIA_ROOT", "/usr/src/paperless/media"),
)

# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("post_consume_v12.15")

# ─── Paperless ENV ────────────────────────────────────────────────────────────

DOCUMENT_ID       = os.environ.get("DOCUMENT_ID", "")
DOCUMENT_FILE_NAME = os.environ.get("DOCUMENT_FILE_NAME", "unbekannt.pdf")
DOCUMENT_SOURCE_PATH = os.environ.get("DOCUMENT_SOURCE_PATH", "")

# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Token {PAPERLESS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _api_url(endpoint: str) -> str:
    """Paperless API-URL ohne doppelte Slashes (api//documents → SPA-HTML)."""
    return f"{PAPERLESS_URL.rstrip('/')}/api/{endpoint.lstrip('/')}"


def _make_retry_session(
    retries: int = 3,
    backoff_factor: float = 0.5,
    status_forcelist: tuple = (500, 502, 503, 504),
) -> requests.Session:
    """HTTP-Session mit automatischem Retry + Exponential Backoff.
    Retries bei transienten Fehlern (5xx, Connection-Fehler).
    Kein Retry bei 4xx (Client-Fehler) — die sind deterministisch.
    """
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods={"GET", "POST", "PATCH", "PUT"},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

# Globale Session — wird für alle Paperless API-Calls wiederverwendet
_http = _make_retry_session()


def paperless_get(endpoint: str, params: dict | None = None) -> dict:
    r = _http.get(
        _api_url(endpoint),
        headers=_headers(),
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    if not r.text or not r.text.strip():
        return {}
    try:
        return r.json()
    except ValueError:
        log.warning("paperless_get %s: keine JSON-Antwort (%s)", endpoint, r.text[:120])
        return {}


def paperless_patch(document_id: int, payload: dict) -> bool:
    r = _http.patch(
        f"{PAPERLESS_URL}/api/documents/{document_id}/",
        json=payload,
        headers=_headers(),
        timeout=30,
    )
    if not r.ok:
        log.error("PATCH /api/documents/%s → %s %s", document_id, r.status_code, r.text[:300])
        return False
    return True


def ensure_dok_id(document_id: int) -> bool:
    """CF Dok-ID = Paperless document id — idempotent, andere Custom Fields bleiben erhalten."""
    if not CF_DOK_ID or not document_id:
        return False
    try:
        doc = paperless_get(f"/documents/{document_id}/")
        if not doc:
            log.warning("ensure_dok_id: Dokument #%s nicht lesbar", document_id)
            return False
        cfs = {
            cf["field"]: cf["value"]
            for cf in doc.get("custom_fields", [])
            if cf.get("field") is not None
        }
        if cfs.get(CF_DOK_ID) == document_id:
            return True
        merged = [{"field": fid, "value": val} for fid, val in cfs.items() if fid != CF_DOK_ID]
        merged.append({"field": CF_DOK_ID, "value": document_id})
        ok = paperless_patch(document_id, {"custom_fields": merged})
        if ok:
            log.info("ensure_dok_id: CF Dok-ID=%s gesetzt", document_id)
        return ok
    except Exception as e:
        log.warning("ensure_dok_id fehlgeschlagen (Dok #%s): %s", document_id, e)
        return False


def paperless_get_notes(document_id: int) -> list:
    """Gibt alle Notizen eines Dokuments zurück."""
    try:
        r = _http.get(
            f"{PAPERLESS_URL}/api/documents/{document_id}/notes/",
            headers=_headers(), timeout=15,
        )
        if r.ok:
            return r.json()
    except Exception as e:
        log.warning("GET notes/%s fehlgeschlagen: %s", document_id, e)
    return []


def paperless_delete_note(document_id: int, note_id: int) -> bool:
    """Löscht eine einzelne Notiz."""
    try:
        r = _http.delete(
            f"{PAPERLESS_URL}/api/documents/{document_id}/notes/{note_id}/",
            headers=_headers(), timeout=15,
        )
        return r.ok
    except Exception as e:
        log.warning("DELETE note/%s/%s fehlgeschlagen: %s", document_id, note_id, e)
        return False


def paperless_post_note(document_id: int, text: str) -> bool:
    """Erstellt eine neue Notiz."""
    try:
        r = _http.post(
            f"{PAPERLESS_URL}/api/documents/{document_id}/notes/",
            json={"note": text},
            headers=_headers(), timeout=15,
        )
        if not r.ok:
            log.warning("POST note/%s → %s %s", document_id, r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        log.warning("POST note/%s fehlgeschlagen: %s", document_id, e)
        return False


_PIPE_NOTE_MARKER = "🤖 pipe v"  # Prefix zur Erkennung von Pipeline-Notizen


def write_pipeline_note(
    document_id: int,
    decision: dict,
    vision_meta: dict,
    pre_decision_used: bool,
    stufe_label: str,
    llm_model: str,
) -> None:
    """Schreibt strukturierte Pipeline-Notiz ins Paperless-Notizfeld.

    Ersetzt vorhandene Pipeline-Notizen (erkennbar am Marker) —
    manuelle Notizen bleiben unangetastet.
    """
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    vision_model = os.environ.get("OLLAMA_VISION_MODEL", "qwen2.5vl:7b")

    # Vision-Felder
    v_absender  = vision_meta.get("absender") or "—"
    v_empfaenger= vision_meta.get("empfaenger") or "—"
    v_datum     = vision_meta.get("datum") or "—"
    v_betrag    = vision_meta.get("betrag") or "—"
    v_doctyp    = vision_meta.get("dokumenttyp_visuell") or "—"
    v_bezahlt   = vision_meta.get("bezahlt")
    v_bezahlt_s = "ja" if v_bezahlt is True else ("nein" if v_bezahlt is False else "—")

    # Entscheidungs-Felder
    confidence  = (decision.get("confidence") or "—").strip()
    korr        = decision.get("korrespondent") or "—"
    ordner      = decision.get("ordner") or "—"
    doctyp      = decision.get("dokumenttyp_semantisch") or "—"
    begruendung = (decision.get("begruendung") or "").strip()
    llm_str     = llm_model if llm_model and not pre_decision_used else "—"

    # Beziehungsinfo falls Stufe 1
    bez_info = ""
    if pre_decision_used and "Beziehung" in stufe_label:
        bez_person = decision.get("_bez_person") or ""
        bez_ref    = decision.get("_bez_ref") or ""
        bez_bez    = decision.get("_bez_bezeichnung") or ""
        parts = []
        if bez_bez:  parts.append(bez_bez)
        if bez_person: parts.append(f"→ {bez_person}")
        if bez_ref:  parts.append(f"| Ref: {bez_ref}")
        if parts:
            bez_info = f"\nBeziehung: {' '.join(parts)}"

    # Review-Hinweis
    review_hint = ""
    if confidence in ("tief", "mittel") or decision.get("_pending_review"):
        review_hint = f"\n⚠ Review-Queue | Confidence: {confidence}"

    sep = "━" * 32
    note_text = (
        f"{_PIPE_NOTE_MARKER}{POST_CONSUME_VERSION} | {now}\n"
        f"{sep}\n"
        f"Stufe:       {stufe_label}\n"
        f"Korrespondent: {korr}\n"
        f"Ordner:      {ordner}\n"
        f"Doctyp:      {doctyp} | Confidence: {confidence}\n"
        f"{sep}\n"
        f"Vision ({vision_model}):\n"
        f"  Absender:  {v_absender}\n"
        f"  Empfänger: {v_empfaenger}\n"
        f"  Datum:     {v_datum} | Betrag: {v_betrag}\n"
        f"  Doctyp:    {v_doctyp} | Bezahlt: {v_bezahlt_s}"
    )
    if bez_info:
        note_text += bez_info
    if begruendung and not pre_decision_used:
        # LLM-Begründung kürzen auf max 120 Zeichen
        bg = begruendung[:120] + ("…" if len(begruendung) > 120 else "")
        note_text += f"\n{sep}\nLLM: {bg}"
    if review_hint:
        note_text += review_hint

    # Vorhandene Pipeline-Notizen löschen
    existing = paperless_get_notes(document_id)
    for n in existing:
        if str(n.get("note", "")).startswith(_PIPE_NOTE_MARKER):
            paperless_delete_note(document_id, n["id"])
            log.info("Pipeline-Notiz #%s ersetzt", n["id"])

    ok = paperless_post_note(document_id, note_text)
    if ok:
        log.info("Pipeline-Notiz geschrieben (Stufe: %s)", stufe_label)
    else:
        log.warning("Pipeline-Notiz konnte nicht geschrieben werden")




# Ollama-Session ohne Retry — LLM-Calls sind idempotent aber langsam
# Retry würde bei Timeout die Laufzeit verdoppeln
_ollama_session = requests.Session()


def ollama_post(endpoint: str, payload: dict, timeout: int) -> dict:
    r = _ollama_session.post(f"{OLLAMA_BASE}/{endpoint}", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def pdf_to_base64_image(pdf_path: str, page: int = 1, dpi: int = 150) -> Optional[str]:
    """PDF-Seite via ghostscript → JPEG → base64."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run([
            "gs", "-dNOPAUSE", "-dBATCH", "-sDEVICE=jpeg",
            f"-dFirstPage={page}", f"-dLastPage={page}", f"-r{dpi}",
            f"-sOutputFile={tmp_path}", pdf_path
        ], capture_output=True, check=True, timeout=120)
        with open(tmp_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        os.unlink(tmp_path)
        return data
    except Exception as e:
        log.warning("pdf_to_base64_image fehlgeschlagen: %s", e)
        return None


def find_pdf(doc_id: str) -> Optional[str]:
    """Legacy: flache ID-Pfade unter MEDIA_ROOT (Fallback)."""
    doc_id_padded = str(doc_id).zfill(7)
    originals = Path(MEDIA_ROOT) / "documents" / "originals" / f"{doc_id_padded}.pdf"
    if originals.exists():
        return str(originals)
    archive = Path(MEDIA_ROOT) / "documents" / "archive" / f"{doc_id_padded}.pdf"
    if archive.exists():
        return str(archive)
    return None


def _download_pdf_via_api(document_id: int) -> Optional[str]:
    """PDF von Paperless-API laden — funktioniert unabhängig vom Dateisystem-Pfad."""
    if not PAPERLESS_TOKEN:
        log.warning("PDF für Dok #%s: kein PAPERLESS_TOKEN für API-Download", document_id)
        return None
    try:
        r = requests.get(
            f"{PAPERLESS_URL.rstrip('/')}/api/documents/{document_id}/download/",
            headers={"Authorization": f"Token {PAPERLESS_TOKEN}"},
            timeout=120,
        )
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(r.content)
            tmp_path = tmp.name
        log.info("PDF für Dok #%s via API geladen (%d bytes)", document_id, len(r.content))
        return tmp_path
    except Exception as e:
        log.warning("PDF API-Download für Dok #%s fehlgeschlagen: %s", document_id, e)
        return None


def _pdf_path_from_paperless_meta(document_id: int) -> Optional[str]:
    """Dateisystem: Speicherpfad + Dateiname aus Paperless-API (wie in der UI)."""
    try:
        doc = paperless_get(f"/documents/{document_id}/")
    except Exception as e:
        log.warning("Dok #%s: API-Metadaten für PDF-Pfad: %s", document_id, e)
        return None

    media = Path(MEDIA_ROOT) / "documents" / "originals"
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
            sp = paperless_get(f"/storage_paths/{sp_id}/")
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
            return str(c)

    if fn:
        base = os.path.basename(fn)
        try:
            for p in media.rglob(base):
                if p.is_file():
                    log.info("PDF für Dok #%s via Suche (%s): %s", document_id, base, p)
                    return str(p)
        except OSError as e:
            log.warning("Dok #%s: rglob(%s): %s", document_id, base, e)
    return None


def resolve_document_pdf(document_id: int | str) -> Optional[str]:
    """PDF für Vision: Pipeline-Pfad → API-Metadaten → Legacy-ID → API-Download."""
    did = int(document_id)

    if DOCUMENT_SOURCE_PATH and Path(DOCUMENT_SOURCE_PATH).exists():
        return DOCUMENT_SOURCE_PATH

    path = _pdf_path_from_paperless_meta(did)
    if path:
        return path

    path = find_pdf(str(did))
    if path:
        return path

    return _download_pdf_via_api(did)


# ─── Manifest laden ───────────────────────────────────────────────────────────

def load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        log.warning("manifest.json nicht gefunden: %s", MANIFEST_PATH)
        return []
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        data = json.load(f)
    # Manifest ist Dict mit "ordner"-Key
    if isinstance(data, dict):
        entries = data.get("ordner", [])
    elif isinstance(data, list):
        entries = data
    else:
        return []
    log.info("Manifest geladen: %d Einträge", len(entries))
    return entries


def load_corrections() -> list[dict]:
    if not CORRECTIONS_PATH.exists():
        return []
    corrections = []
    with open(CORRECTIONS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    corrections.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return corrections


# ─── RAG: bge-m3 Embeddings ───────────────────────────────────────────────────

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def get_embedding(text: str) -> Optional[list[float]]:
    try:
        resp = ollama_post(
            "api/embeddings",
            {"model": MODEL_EMBED, "prompt": text[:4096]},
            timeout=60,
        )
        return resp.get("embedding")
    except Exception as e:
        log.warning("bge-m3 Embedding fehlgeschlagen: %s", e)
        return None


def manifest_entry_to_corpus(entry: dict) -> str:
    """
    Baut einen repräsentativen Text aus einem Manifest-Eintrag.
    Nutzt die tatsächliche Struktur von manifest.json v1.1:
      - pfad (NICHT ordner!)
      - beschreibung
      - erkennungsmerkmale: visuell, layout_hinweis, bereich_absender,
                            bereich_inhalt, bereich_empfaenger, kennzeichen,
                            schweiz_spezifisch
      - dokumenttyp: primär, auch
      - abgrenzung
    """
    erk = entry.get("erkennungsmerkmale", {})

    # Alle erkennungsmerkmale-Felder zusammenführen
    visuell       = erk.get("visuell", "")
    layout        = erk.get("layout_hinweis", "")
    absender      = erk.get("bereich_absender", "")
    inhalt        = erk.get("bereich_inhalt", "")
    empfaenger    = erk.get("bereich_empfaenger", "")
    kennzeichen   = erk.get("kennzeichen", "")
    ch_spezifisch = erk.get("schweiz_spezifisch", "")
    fahrzeug      = erk.get("fahrzeug", "")

    # Listen zu Strings
    def to_str(v):
        if isinstance(v, list):
            return " ".join(v)
        return str(v) if v else ""

    # Dokumenttyp
    doctyp = entry.get("dokumenttyp", {})
    if isinstance(doctyp, dict):
        primaer = doctyp.get("primär", "")
        auch    = " ".join(doctyp.get("auch", []))
    else:
        primaer = str(doctyp)
        auch    = ""

    corpus = " ".join(filter(None, [
        entry.get("pfad", ""),           # PFAD (nicht ordner)
        entry.get("beschreibung", ""),
        to_str(visuell),
        to_str(layout),
        to_str(absender),
        to_str(inhalt),
        to_str(empfaenger),
        to_str(kennzeichen),
        to_str(ch_spezifisch),
        to_str(fahrzeug),
        primaer,
        auch,
        to_str(entry.get("abgrenzung", "")),
    ]))
    return corpus.strip()


EMBEDDING_CACHE_PATH = MANIFEST_PATH.parent / "manifest_embeddings.json"


def _manifest_hash(manifest: list[dict]) -> str:
    """SHA256 des Manifest-Inhalts für Cache-Validierung."""
    import hashlib
    content = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def load_or_build_manifest_embeddings(manifest: list[dict]) -> dict[str, list[float]]:
    """
    Lädt Manifest-Embeddings aus Cache oder berechnet sie neu.
    Cache wird invalidiert wenn manifest.json Inhalt sich geändert hat (Hash).
    """
    current_hash = _manifest_hash(manifest)
    cache_data: dict = {}

    # Cache laden falls vorhanden
    if EMBEDDING_CACHE_PATH.exists():
        try:
            with open(EMBEDDING_CACHE_PATH, encoding="utf-8") as f:
                cache_data = json.load(f)
            cached_hash = cache_data.get("manifest_hash", "")
            if cached_hash == current_hash:
                embeddings = cache_data.get("embeddings", {})
                log.info("Manifest-Embedding-Cache gültig: %d Einträge (hash=%s)", len(embeddings), current_hash)
                return embeddings
            else:
                log.info("Manifest geändert (hash %s → %s) — Cache neu berechnen", cached_hash, current_hash)
        except Exception as e:
            log.warning("Cache laden fehlgeschlagen: %s — neu berechnen", e)

    # Neu berechnen
    log.info("Berechne Manifest-Embeddings (%d Einträge) ...", len(manifest))
    embeddings: dict[str, list[float]] = {}
    for entry in manifest:
        pfad   = entry.get("pfad", "")
        if not pfad:
            continue
        corpus = manifest_entry_to_corpus(entry)
        emb    = get_embedding(corpus)
        if emb:
            embeddings[pfad] = emb
        else:
            log.warning("Kein Embedding für '%s'", pfad)

    # Cache speichern
    try:
        with open(EMBEDDING_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {"manifest_hash": current_hash, "embeddings": embeddings},
                f,
                ensure_ascii=False,
            )
        log.info("Manifest-Embedding-Cache gespeichert: %d Einträge (hash=%s)", len(embeddings), current_hash)
    except Exception as e:
        log.warning("Cache speichern fehlgeschlagen: %s", e)

    return embeddings


def find_similar_manifest_entries(
    text: str,
    manifest: list[dict],
    manifest_embeddings: dict[str, list[float]],
    top_k: int,
) -> list[dict]:
    if not manifest:
        return []

    query_emb = get_embedding(text)
    if query_emb is None:
        log.warning("Kein Query-Embedding — RAG Fallback: erste %d Einträge", top_k)
        return manifest[:top_k]

    # Index: pfad → entry
    entry_map = {e.get("pfad", ""): e for e in manifest}

    scored = []
    for pfad, entry_emb in manifest_embeddings.items():
        entry = entry_map.get(pfad)
        if entry is None:
            continue
        sim = cosine_similarity(query_emb, entry_emb)
        scored.append((sim, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    log.info(
        "RAG Top-%d: %s",
        top_k,
        [(round(s, 3), e.get("pfad", "?")) for s, e in scored[:top_k]]
    )
    return [e for _, e in scored[:top_k]]


def _find_json_object(text: str) -> str:
    """
    Findet das erste vollständige JSON-Objekt via Brace-Counter.
    Korrekt bei nested JSON — Regex ist es nicht.
    """
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start:i + 1]
    return ""


def extract_json_from_response(raw: str) -> dict:
    """Robustes JSON-Parsing — toleriert Markdown, Reasoning-Text, nested JSON."""
    import re
    raw = raw.strip()
    # Thinking-Tags entfernen (qwen3 reasoning)
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
    # 1. Direkt als JSON parsen (bester Fall)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 2. Markdown-Block entfernen und erneut versuchen
    md_match = re.search(r'```(?:json)?\s*(.+?)\s*```', raw, re.DOTALL)
    if md_match:
        try:
            return json.loads(md_match.group(1))
        except json.JSONDecodeError:
            pass
    # 3. Brace-Counter — findet erstes vollständiges JSON-Objekt
    candidate = _find_json_object(raw)
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    log.warning("JSON-Extraktion fehlgeschlagen. Raw: %s", raw[:200])
    return {}


def normalize_decision_keys(d: dict) -> dict:
    """
    Normalisiert JSON-Keys auf Kleinschreibung.
    llama3:8b liefert manchmal 'Ordner' statt 'ordner', 'Tags' statt 'tags'.
    """
    KEY_MAP = {
        "ordner":                 ["ordner", "Ordner", "folder"],
        "tags":                   ["tags", "Tags"],
        "korrespondent":          ["korrespondent", "Korrespondent", "absender"],
        "titel":                  ["titel", "Titel", "title"],
        "datum":                  ["datum", "Datum", "date"],
        "betrag":                 ["betrag", "Betrag", "amount"],
        "dokumenttyp_semantisch": ["dokumenttyp_semantisch", "dokumenttyp", "Dokumenttyp", "type"],
        "confidence":             ["confidence", "Confidence"],
        "begruendung":            ["begruendung", "Begründung", "reason"],
    }
    result = {}
    for canonical, variants in KEY_MAP.items():
        for v in variants:
            if v in d:
                result[canonical] = d[v]
                break
    return result


VISION_SYSTEM = "Du bist ein JSON-Extraktor für Schweizer Dokumente. Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt. Kein Text davor oder danach. Kein Markdown."


def _disambiguate_vision_money_fields(vision_meta: dict) -> dict:
    """Trennt Schweizer Rechnungsnummern (z.B. Zürich 50.699.251.081) vom Zahlungsbetrag.

    Auf Prämienrechnungen steht «Rechnung: 50.699.251.081» — das ist keine CHF-Summe.
    """
    if not vision_meta:
        return vision_meta
    import re as _re
    meta = dict(vision_meta)
    betrag = meta.get("betrag")
    if not betrag or str(betrag).lower() in ("null", "none", ""):
        return meta
    raw = str(betrag).strip()
    num = _re.sub(r"[^\d.]", "", raw.replace("'", ""))
    is_rechnungsnr = False
    if _re.fullmatch(r"\d{1,3}(\.\d{3}){2,}", num):
        is_rechnungsnr = True
    elif num.count(".") >= 2 and _re.fullmatch(r"[\d.]+", num):
        is_rechnungsnr = True
    else:
        try:
            if float(num.replace(",", ".")) > 500_000:
                is_rechnungsnr = True
        except ValueError:
            pass
    if is_rechnungsnr:
        if not (meta.get("rechnungsnummer") or "").strip():
            meta["rechnungsnummer"] = num or raw
        meta["betrag"] = None
        log.info("Vision: '%s' als Rechnungsnummer erkannt (kein Betrag)", raw)
    return meta


def vision_analyze(image_b64: Optional[str], ocr_text: str) -> dict:
    """Vision-LLM Analyse — Ollama natives Format (images-Array, nicht image_url)."""

    # Haushalt-Kontext für Vision-Prompt aufbauen
    personen_namen = _get_haushalt_personen_namen()
    beziehungen    = _build_beziehungen_map()

    haushalt_kontext = ""
    if personen_namen:
        haushalt_kontext += (
            f"\nHAUSHALT-KONTEXT (wichtig für Absender-Erkennung):\n"
            f"Haushaltsmitglieder (sind NIEMALS der Absender, immer der Empfänger): "
            f"{', '.join(personen_namen)}\n"
        )
    if beziehungen:
        arbeitgeber = [b for b in beziehungen if b.get("typ") == "arbeitgeber"]
        if arbeitgeber:
            ag_liste = ", ".join(
                f"{b['korrespondent']} (Arbeitgeber von {b.get('person','')})"
                for b in arbeitgeber
            )
            haushalt_kontext += (
                f"Bekannte Arbeitgeber: {ag_liste}\n"
                f"→ Bei Lohnausweis/Lohnabrechnung: Arbeitgeber = Absender, "
                f"Haushaltsmitglied = Empfänger\n"
            )

    user_content = (
        f"Extrahiere aus diesem Schweizer Dokument folgende Felder als JSON.\n"
        f"Achte besonders auf handschriftliche Notizen oben rechts am Rand "
        f"(meist ein Bezahlt-Vermerk wie 'bez. 6.2.26' oder 'bez 26.3.26' oder 'EZ 26.3.26').\n"
        f"{haushalt_kontext}\n"
        f'{{"absender": "Firmenname oder Behörde nicht Empfänger", '
        f'"empfaenger": "Name des Empfängers", '
        f'"datum": "{DATUM_PROMPT_HINT}", '
        f'"betrag": "Zahlungsbetrag CHF XX.XX oder null — NICHT die Rechnungsnummer", '
        f'"rechnungsnummer": "Rechnungs-/Fakturanummer z.B. 50.699.251.081 oder null", '
        f'"kennzeichen": "Fahrzeugkennzeichen z.B. AG 239878 oder null", '
        f'"dokumenttyp_visuell": "z.B. Rechnung/Lohnabrechnung/Verfügung", '
        f'"layout": "Beschreibung des Layouts", '
        f'"logo_vorhanden": true/false, '
        f'"tabellen_vorhanden": true/false, '
        f'"qr_einzahlungsschein": true/false, '
        f'"sprache": "de/fr/it/en", '
        f'"handschrift": "handschriftliche Notiz exakt abschreiben z.B. bez. 6.2.26 — null wenn keine", '
        f'"besonderheiten": "wichtige Zusatzinfos oder null"}}\n\n'
        f"OCR-Text (Zusatzinfo):\n{ocr_text[:1500]}"
    )

    if image_b64:
        messages = [{
            "role": "user",
            "content": user_content,
            "images": [image_b64],
        }]
    else:
        messages = [{
            "role": "user",
            "content": user_content,
        }]

    try:
        resp = ollama_post(
            "api/chat",
            {
                "model": MODEL_VISION,
                "messages": messages,
                "system": VISION_SYSTEM,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1, "num_predict": 300},
            },
            timeout=VISION_TIMEOUT,
        )
        raw = resp.get("message", {}).get("content", "")
        return extract_json_from_response(raw)
    except Exception as e:
        log.warning("Vision-LLM fehlgeschlagen: %s", e)
        return {}


def vision_schulbericht_page(
    image_b64: str,
    ocr_text: str,
    page: int,
    page_total: int,
) -> dict:
    """E2E: eine Seite → Schulbericht-JSON (Debug/Vergleich)."""
    if not image_b64:
        return {}
    user_content = build_schulbericht_vision_prompt(ocr_text, page, page_total)
    try:
        resp = ollama_post(
            "api/chat",
            {
                "model": MODEL_VISION,
                "messages": [{
                    "role": "user",
                    "content": user_content,
                    "images": [image_b64],
                }],
                "system": SCHULBERICHT_VISION_SYSTEM,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0, "num_predict": SCHULBERICHT_NUM_PREDICT},
            },
            timeout=VISION_TIMEOUT,
        )
        raw = resp.get("message", {}).get("content", "")
        data = extract_json_from_response(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("Schulbericht-E2E Seite %d/%d fehlgeschlagen: %s", page, page_total, e)
        return {}


def vision_htr_page(image_b64: str, page: int, page_total: int, variant_id: str = "full") -> dict:
    """Stufe 1: zeilengetreue HTR einer Seite (optional pro Crop-Variante)."""
    if not image_b64:
        return {}
    user_content = build_htr_transcribe_prompt(page, page_total)
    try:
        resp = ollama_post(
            "api/chat",
            {
                "model": MODEL_VISION,
                "messages": [{
                    "role": "user",
                    "content": user_content,
                    "images": [image_b64],
                }],
                "system": HTR_VISION_SYSTEM,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0, "num_predict": HTR_NUM_PREDICT},
            },
            timeout=VISION_TIMEOUT,
        )
        raw = resp.get("message", {}).get("content", "")
        data = parse_htr_response(raw)
        if data:
            data["_variant_id"] = variant_id
        return data
    except Exception as e:
        log.warning(
            "Schulbericht-HTR Seite %d/%d [%s] fehlgeschlagen: %s",
            page, page_total, variant_id, e,
        )
        return {}


def extract_schulbericht_from_transcript(transcript: str) -> dict:
    """Stufe 2: Schulbericht-Felder aus Transkription (Text-LLM, kein Bild)."""
    if not transcript.strip():
        return {}
    model = os.environ.get("SCHULBERICHT_EXTRACT_MODEL", MODEL_LLM)
    prompt = build_extract_from_transcript_prompt(transcript)
    try:
        resp = ollama_post(
            "api/chat",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "system": EXTRACT_SYSTEM,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0, "num_predict": 1024},
            },
            timeout=LLM_TIMEOUT,
        )
        raw = resp.get("message", {}).get("content", "")
        data = extract_json_from_response(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("Schulbericht-Extract fehlgeschlagen: %s", e)
        return {}


def _schulbericht_pdf_to_b64(pdf_path: str, page: int) -> Optional[str]:
    return pdf_to_base64_image(pdf_path, page=page, dpi=SCHULBERICHT_DPI)


def vision_schulbericht_multipage(pdf_path: str, ocr_text: str) -> dict:
    """E2E mehrseitig (Fallback / Debug)."""
    return analyze_schulbericht_pdf(
        pdf_path,
        ocr_text,
        pdf_to_b64=_schulbericht_pdf_to_b64,
        vision_page=vision_schulbericht_page,
    )


def vision_schulbericht_pipeline(pdf_path: str) -> dict:
    """Produktiv: HTR → Extract. Fallback auf E2E wenn HTR leer."""
    sb = analyze_schulbericht_two_stage(
        pdf_path,
        pdf_to_b64=_schulbericht_pdf_to_b64,
        htr_page=vision_htr_page,
        extract_from_text=extract_schulbericht_from_transcript,
    )
    if sb:
        return sb
    log.warning("Schulbericht-Pipeline: HTR/Extract leer — Fallback E2E")
    return vision_schulbericht_multipage(pdf_path, "")


def vision_brillenpass_analyze(
    image_b64: Optional[str], ocr_text: str, parser_hint: dict | None = None,
) -> dict:
    """Stufe 2: gezielter Vision-Call nur für Brillenpass-Kacheln (nicht ganzes Dokument)."""
    if not image_b64:
        log.warning(
            "Brillenpass Stufe 2 übersprungen — kein PDF-Bild "
            "(Vision ohne Bild erzeugt erfundene Werte)"
        )
        return {}
    user_content = build_brillenpass_vision_prompt(ocr_text, parser_hint)
    messages = [{"role": "user", "content": user_content, "images": [image_b64]}]
    try:
        from brillenpass_parser import normalize_vision_brillenpass  # noqa: WPS433

        resp = ollama_post(
            "api/chat",
            {
                "model": MODEL_VISION,
                "messages": messages,
                "system": VISION_SYSTEM,
                "stream": False,
                "format": REFRAKTION_JSON_SCHEMA,
                "options": {"temperature": 0, "seed": 42, "num_predict": 500},
            },
            timeout=VISION_TIMEOUT,
        )
        raw = resp.get("message", {}).get("content", "")
        data = extract_json_from_response(raw)
        if not isinstance(data, dict):
            return {}
        return normalize_vision_brillenpass(data, parser_hint=parser_hint, ocr_text=ocr_text)
    except Exception as e:
        log.warning("Vision Brillenpass fehlgeschlagen: %s", e)
        return {}


def _brillenpass_missing_fern_eye(data: dict | None) -> bool:
    if not data or not has_brillenpass_values(data):
        return True
    fern = data.get("fern") or {}
    r = (fern.get("rechts") or {}).get("sph")
    l = (fern.get("links") or {}).get("sph")
    return bool(r) != bool(l) or not (r and l)


def _brillenpass_suspicious_primary(data: dict | None) -> bool:
    if not data or not has_brillenpass_values(data):
        return True
    from brillenpass_parser import plausible_brillenpass_data, _cross_eye_suspicious  # noqa: WPS433
    if not plausible_brillenpass_data(data):
        return True
    if _cross_eye_suspicious(data):
        return True
    return False


def should_use_brillenpass_vision_fallback(
    header_anchors: int,
    primary_data: dict | None,
    *,
    has_image: bool,
) -> bool:
    """Vision bei Lücken, Müll-Werten oder widersprüchlichen Augen."""
    if not has_image:
        return False
    if BRILLENPASS_VISION_ON_GAPS and _brillenpass_suspicious_primary(primary_data):
        return True
    if not BRILLENPASS_VISION_FALLBACK:
        return False
    if header_anchors >= BRILLENPASS_MIN_HEADER_ANCHORS:
        return False
    if primary_data and has_brillenpass_values(primary_data):
        return False
    return True


def _brillenpass_extraction_confidence(
    tsv_meta: dict,
    regex_data: dict | None,
    *,
    vision_used: bool,
    merged: dict | None,
) -> str:
    if vision_used:
        return "niedrig"
    if has_brillenpass_values(merged or {}):
        tsv_conf = tsv_meta.get("confidence")
        if tsv_conf:
            return tsv_conf
        if tsv_meta.get("header_anchors", 0) >= BRILLENPASS_MIN_HEADER_ANCHORS:
            return "hoch"
        if regex_data and has_brillenpass_values(regex_data):
            return "mittel"
        return tsv_meta.get("confidence") or "mittel"
    return tsv_meta.get("confidence") or "keine_extraktion"


def run_brillenpass_extraction_stages(
    document_id: int | None,
    ocr_text: str,
    parser_names: list,
    dt_vis: str,
    vision_meta: dict | None,
    *,
    pdf_path: str | None = None,
    image_b64: Optional[str] = None,
) -> dict:
    """Stufe 1 TSV + Regex, Stufe 2 Vision nur bei BRILLENPASS_VISION_FALLBACK=1."""
    from brillenpass_tsv import (  # noqa: WPS433
        extract_brillenpass_from_image,
        merge_brillenpass_tsv_with_regex,
    )

    chosen = detect_parser(
        ocr_text, allowed=parser_names, dokumenttyp_visuell=dt_vis, vision_meta=vision_meta,
    )

    tsv_data: dict = {}
    tsv_meta: dict = {"enabled": BRILLENPASS_TESSERACT, "header_anchors": 0, "confidence": "keine_extraktion"}
    if BRILLENPASS_TESSERACT and pdf_path:
        log.info("Brillenpass Stufe 1a: Tesseract TSV (Dok #%s)", document_id)
        tsv_data, _tsv_conf, tsv_meta = extract_brillenpass_from_image(pdf_path, parser_names)
        tsv_meta["enabled"] = True
    elif not BRILLENPASS_TESSERACT:
        log.info("Brillenpass Stufe 1a: Tesseract deaktiviert (BRILLENPASS_TESSERACT=0)")
    elif not pdf_path:
        log.warning("Brillenpass Stufe 1a: kein PDF — Tesseract übersprungen (Dok #%s)", document_id)

    if document_id:
        write_audit_entry(document_id, "brillenpass_s1_tsv", {
            "header_anchors": tsv_meta.get("header_anchors", 0),
            "header_fields": tsv_meta.get("header_fields", []),
            "method": tsv_meta.get("method"),
            "word_count": tsv_meta.get("word_count", 0),
            "confidence": tsv_meta.get("confidence"),
            "red_channel": bool(tsv_meta.get("red_channel")),
            "snapshot": snapshot_brillenpass(tsv_data),
        })

    log.info("Brillenpass Stufe 1b: OCR-Regex-Parser")
    regex_data = parse_brillenpass_with_parsers(
        ocr_text, parser_names, dokumenttyp_visuell=dt_vis, vision_meta=vision_meta,
    )
    if not regex_data and "fielmann_rechnung" in parser_names:
        regex_data = parse_fielmann_brillenpass(ocr_text)

    if document_id:
        write_audit_entry(document_id, "brillenpass_s1", {
            "parser": chosen,
            "snapshot": snapshot_brillenpass(regex_data),
        })

    parser_data = merge_brillenpass_tsv_with_regex(tsv_data, regex_data, ocr_text=ocr_text)
    header_anchors = int(tsv_meta.get("header_anchors") or 0)

    if not image_b64 and pdf_path:
        image_b64 = pdf_to_base64_image(pdf_path)

    vision_used = should_use_brillenpass_vision_fallback(
        header_anchors, parser_data, has_image=bool(image_b64),
    )
    vision_bp: dict = {}
    if vision_used:
        log.info(
            "Brillenpass Stufe 2: Vision-Fallback (%s, Anker=%s) Dok #%s",
            MODEL_VISION, header_anchors, document_id,
        )
        vision_bp = vision_brillenpass_analyze(image_b64, ocr_text, parser_data)
    elif not BRILLENPASS_VISION_FALLBACK:
        log.info("Brillenpass Stufe 2: Vision deaktiviert (BRILLENPASS_VISION_FALLBACK=0) Dok #%s", document_id)
    elif header_anchors >= BRILLENPASS_MIN_HEADER_ANCHORS:
        log.info(
            "Brillenpass Stufe 2: Vision übersprungen — %s Header-Anker (Dok #%s)",
            header_anchors, document_id,
        )
    elif parser_data and has_brillenpass_values(parser_data):
        log.info("Brillenpass Stufe 2: Vision übersprungen — Stufe 1 ausreichend (Dok #%s)", document_id)
    elif not image_b64:
        log.warning("Brillenpass Stufe 2: kein PDF-Bild — Vision übersprungen (Dok #%s)", document_id)

    if document_id:
        write_audit_entry(document_id, "brillenpass_s2", {
            "has_image": bool(image_b64),
            "vision_enabled": bool(BRILLENPASS_VISION_FALLBACK or BRILLENPASS_VISION_ON_GAPS),
            "vision_on_gaps": BRILLENPASS_VISION_ON_GAPS,
            "vision_used": vision_used,
            "header_anchors": header_anchors,
            "snapshot": snapshot_brillenpass(vision_bp),
            "vision_empty": not vision_bp,
        })

    prefer_vis = (
        prefer_vision_for_brillenpass_merge(parser_data, vision_bp, has_image=bool(image_b64))
        if vision_used else False
    )
    merged = merge_brillenpass(parser_data, vision_bp, prefer_vision=prefer_vis)
    confidence = _brillenpass_extraction_confidence(
        tsv_meta, regex_data, vision_used=vision_used, merged=merged,
    )
    merged.setdefault("extraktion", {})["confidence"] = confidence

    return {
        "chosen": chosen,
        "tsv_data": tsv_data,
        "regex_data": regex_data,
        "parser_data": parser_data,
        "vision_bp": vision_bp,
        "prefer_vis": prefer_vis,
        "merged": merged,
        "tsv_meta": tsv_meta,
        "vision_used": vision_used,
    }


def _load_brillenpaesse_data() -> dict:
    if not BRILLENPAESSE_PATH.exists():
        return {"version": "1.0", "eintraege": []}
    try:
        return json.loads(BRILLENPAESSE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("brillenpaesse.json laden fehlgeschlagen: %s", e)
        return {"version": "1.0", "eintraege": []}


def _get_letzte_brillenpass_version(person_id: str) -> dict | None:
    for entry in _load_brillenpaesse_data().get("eintraege", []):
        if entry.get("person_id") == person_id:
            vers = entry.get("versionen") or []
            from brillenpass_parser import latest_brillenpass_version  # noqa: WPS433
            return latest_brillenpass_version(vers)
    return None


def write_pending_brillenpass(
    vorschlag: dict,
    person_id: str,
    anzeigename: str,
    korrespondent_name: str,
    document_id: int | None = None,
    source: str = "pipeline",
) -> bool:
    """Brillenpass-Vorschlag in pending_brillenpass.jsonl — Dedupe pro document_id oder manuell."""
    import time as _time
    import fcntl as _fcntl

    PENDING_BRILLENPASS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if PENDING_BRILLENPASS_PATH.exists():
        lines = [
            ln for ln in PENDING_BRILLENPASS_PATH.read_text(encoding="utf-8").split("\n") if ln.strip()
        ]
        gueltig_ab = (vorschlag or {}).get("gueltig_ab")
        for ln in lines:
            try:
                e = json.loads(ln)
                if e.get("status") != "pending":
                    continue
                if document_id and e.get("document_id") == document_id:
                    log.info("Brillenpass pending bereits vorhanden für Dok #%s — übersprungen", document_id)
                    return False
                if not document_id and source == "manual":
                    ev = e.get("vorschlag") or {}
                    if (
                        e.get("source") == "manual"
                        and e.get("person_id") == person_id
                        and e.get("korrespondent") == korrespondent_name
                        and ev.get("gueltig_ab") == gueltig_ab
                    ):
                        log.info("Brillenpass manual pending bereits vorhanden — übersprungen")
                        return False
            except Exception:
                continue

    entry = {
        "status":           "pending",
        "source":           source,
        "timestamp":        _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "document_id":      document_id,
        "person_id":        person_id,
        "anzeigename":      anzeigename,
        "korrespondent":    korrespondent_name,
        "vorschlag":        vorschlag,
        "letzte_version":   _get_letzte_brillenpass_version(person_id),
    }
    with open(PENDING_BRILLENPASS_PATH, "a", encoding="utf-8") as f:
        _fcntl.flock(f, _fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        finally:
            _fcntl.flock(f, _fcntl.LOCK_UN)
    log.info(
        "Pending-Brillenpass: person=%s korrespondent=%s dok=%s",
        person_id, korrespondent_name, document_id,
    )
    return True


def write_pending_htr_decision(document_id: int, resolution_audit: dict) -> None:
    """HTR-Defer in pending_htr_decision.jsonl — Dedupe pro document_id."""
    import fcntl as _fcntl

    PENDING_HTR_DECISION_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "document_id": document_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **resolution_audit,
    }
    lines: list[str] = []
    if PENDING_HTR_DECISION_PATH.exists():
        for ln in PENDING_HTR_DECISION_PATH.read_text(encoding="utf-8").split("\n"):
            if not ln.strip():
                continue
            try:
                row = json.loads(ln)
                if row.get("document_id") != document_id:
                    lines.append(ln)
            except json.JSONDecodeError:
                lines.append(ln)
    lines.append(json.dumps(entry, ensure_ascii=False))
    with open(PENDING_HTR_DECISION_PATH, "w", encoding="utf-8") as f:
        _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
        try:
            f.write("\n".join(lines) + "\n")
        finally:
            _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
    log.info("Pending-HTR: Dok #%s → pending_htr_decision", document_id)


def maybe_queue_brillenpass(
    document_id: int,
    ocr_text: str,
    vision_meta: dict | None,
    corr_entry: dict | None,
    image_b64: Optional[str] = None,
) -> None:
    """Optiker mit brillenpass.aktiv → Stufe 1 Parser + Stufe 2 Vision → Review-Queue."""
    if not corr_entry:
        return
    aktiv, _ = corr_supports_brillenpass(corr_entry)
    if not aktiv:
        return
    parser_names = corr_brillenpass_parsers(corr_entry)

    dt_vis = (vision_meta or {}).get("dokumenttyp_visuell", "")
    if not should_trigger_brillenpass(ocr_text, parser_names, dt_vis, vision_meta):
        log.info("Brillenpass: kein Trigger für Dok #%s (Korr=%s)", document_id, corr_entry.get("name"))
        write_audit_entry(document_id, "brillenpass_skip", {
            "reason": "no_trigger", "parser_names": parser_names,
        })
        return

    direct_name, direct_reason = _match_person_direct(ocr_text, vision_meta)
    if not direct_name:
        log.info("Brillenpass: Person nicht eindeutig — Dok #%s übersprungen", document_id)
        write_audit_entry(document_id, "brillenpass_skip", {"reason": "person_ambiguous"})
        return
    person_id = _resolve_person_id(direct_name)
    anzeigename = _resolve_person_anzeigename(person_id) or direct_name

    pdf_path = resolve_document_pdf(document_id)
    stages = run_brillenpass_extraction_stages(
        document_id,
        ocr_text,
        parser_names,
        dt_vis,
        vision_meta,
        pdf_path=pdf_path,
        image_b64=image_b64,
    )
    chosen = stages["chosen"]
    parser_data = stages["parser_data"]
    vision_bp = stages["vision_bp"]
    prefer_vis = stages["prefer_vis"]
    merged = stages["merged"]
    log.info(
        "Brillenpass-Trigger: person=%s (%s), parser=%s → %s",
        person_id, direct_reason, parser_names, chosen,
    )
    merged["korrespondent"] = corr_entry.get("name", "")
    if not merged.get("gueltig_ab"):
        from brillenpass_parser import _parse_pass_date, normalize_gueltig_ab_iso  # noqa: WPS433
        merged["gueltig_ab"] = _parse_pass_date(ocr_text) or normalize_gueltig_ab_iso(
            (vision_bp or {}).get("gueltig_ab")
        )
    if not merged.get("gueltig_ab") and vision_meta:
        merged["gueltig_ab"] = vision_meta.get("datum")

    diagnose = diagnose_brillenpass_extraction(
        parser_data, vision_bp, merged,
        parser_detected=chosen,
        has_image=bool(image_b64 or pdf_path),
        prefer_vision=prefer_vis,
    )
    merged.setdefault("extraktion", {})["diagnose"] = diagnose
    write_audit_entry(document_id, "brillenpass_merged", diagnose)
    if diagnose.get("gaps"):
        log.warning(
            "Brillenpass Lücken Dok #%s: %s | S1=%s S2=%s",
            document_id, ", ".join(diagnose["gaps"]),
            "ok" if diagnose.get("stufe1_ok") else "leer/teil",
            "ok" if diagnose.get("stufe2_ok") else "leer/teil",
        )
    else:
        log.info("Brillenpass vollständig Dok #%s (Parser=%s)", document_id, chosen)

    if not has_brillenpass_values(merged):
        log.info("Brillenpass: keine verwertbaren Werte — Dok #%s übersprungen", document_id)
        write_audit_entry(document_id, "brillenpass_skip", {"reason": "no_values", "diagnose": diagnose})
        return

    if not write_pending_brillenpass(
        merged, person_id, anzeigename, corr_entry.get("name", ""), document_id=document_id,
    ):
        return

    tag_id = _get_by_name("tags", PENDING_BRILLENPASS_TAG) or _create_obj("tags", PENDING_BRILLENPASS_TAG)
    if tag_id:
        try:
            doc = paperless_get(f"/documents/{document_id}/")
            tags = list(doc.get("tags") or [])
            if tag_id not in tags:
                tags.append(tag_id)
                paperless_patch(document_id, {"tags": tags})
        except Exception as e:
            log.warning("Brillenpass-Tag setzen fehlgeschlagen: %s", e)


def parse_handschrift_bezahlt(handschrift):
    """Extrahiert Bezahlt-Datum aus handschriftlicher Notiz.
    Erkennt: bez. 6.2.26, bez 26.3.2026, BEZ 6.2.26, bezahlt 6.2.26, bz. 6.2.26
    NICHT erkannt: EZ (= Einzahlung, kein Bezahlt-Vermerk — False Positive vermeiden)
    Gibt ISO-Datum zurück (YYYY-MM-DD) oder None.
    """
    if not handschrift:
        return None
    import re as _re
    m = _re.search(
        r'(?:bezahlt\s*|bez\.?\s*|bz\.?\s*)(\d{1,2})[.\s/](\d{1,2})[.\s/](\d{2,4})',
        handschrift.lower()
    )
    if not m:
        return None
    day, month, year = m.group(1), m.group(2), m.group(3)
    if len(year) == 2:
        year = "20" + year
    try:
        import datetime as _dt
        d = _dt.date(int(year), int(month), int(day))
        return d.isoformat()
    except ValueError:
        return None


# ─── Schritt 4: qwen3:32b Entscheidung ───────────────────────────────────────

def build_constraints(similar_entries: list[dict], target_ordner: str = None) -> dict:
    """
    Berechnet Constraints aus:
    1. Zielordner (primär) — wenn bekannt
    2. Top-3 RAG-Nachbarn (sekundär) — nur als Ergänzung
    3. tags_global (immer)
    """
    top3 = similar_entries[:3]
    allowed_tags: set[str] = set()
    allowed_dokumenttypen: set[str] = set()
    verboten_tags: set[str] = set()

    # tags_global immer laden
    try:
        manifest_root = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        for gruppe in manifest_root.get("tags_global", {}).values():
            allowed_tags.update(gruppe)
        # Wenn Zielordner bekannt: primär dessen Tags verwenden
        if target_ordner:
            for e in manifest_root.get("ordner", []):
                if e.get("pfad") == target_ordner:
                    allowed_tags.update(e.get("erlaubte_tags", []))
                    allowed_dokumenttypen.update(e.get("erlaubte_dokumenttypen", []))
                    verboten_tags.update(e.get("verbotene_tags", []))
                    break
    except Exception:
        pass

    # RAG-Nachbarn als Ergänzung (nicht als primäre Quelle)
    for e in top3:
        # Nur Tags hinzufügen die NICHT verboten sind
        for t in e.get("erlaubte_tags", []):
            if t not in verboten_tags:
                allowed_tags.add(t)
        allowed_dokumenttypen.update(e.get("erlaubte_dokumenttypen", []))
        # Verbotene Tags aus Top-1 haben Vorrang
    verboten_tags.update(top3[0].get("verbotene_tags", []) if top3 else [])

    allowed_tags -= verboten_tags
    max_tags = min((e.get("max_tags", 4) for e in top3), default=4)
    return {
        "allowed_tags": allowed_tags,
        "verboten_tags": verboten_tags,
        "allowed_dokumenttypen": allowed_dokumenttypen,
        "max_tags": max_tags,
    }


def build_llm_prompt(
    ocr_text: str,
    vision_meta: dict,
    similar_entries: list[dict],
    manifest: list[dict],
    corrections: list[dict],
    filename: str,
    constraints: dict,
    corr_map: dict = None,
    stufe1_kontext: dict = None,
) -> str:
    """Optimierter Prompt — kompakt und fokussiert.

    Optimierungen gegenüber Vorgänger:
    - Ordnerliste: nur Top-5 RAG-Kandidaten + alle passenden Ordner
      statt vollständige Liste (verhindert Aufmerksamkeitsverlust)
    - OCR: 2000 statt 3000 Zeichen (erste 2000 enthalten fast immer Absender+Betreff)
    - Vision: nur relevante Felder, kein JSON-Dump des ganzen Objekts
    - Korrekturen: nur letzte 5 statt 8
    - Struktur: Constraints direkt nach Vision (höhere Aufmerksamkeit am Anfang)
    - stufe1_kontext: Beziehungen + verbotene_* aus correspondents.json
    """
    allowed_tags          = constraints["allowed_tags"]
    verboten_tags         = constraints["verboten_tags"]
    allowed_dokumenttypen = constraints["allowed_dokumenttypen"]
    max_tags              = constraints["max_tags"]

    # Ordnerliste: RAG-Kandidaten zuerst, dann Rest — kompakter als alle
    rag_ordner   = [e.get("pfad", "?") for e in similar_entries]
    alle_ordner  = [e.get("pfad", "?") for e in manifest]
    # Top-Kandidaten + Rest (dedupliziert, Reihenfolge erhalten)
    seen = set()
    ordner_priorisiert = []
    for o in rag_ordner + alle_ordner:
        if o not in seen:
            ordner_priorisiert.append(o)
            seen.add(o)

    # Vision: nur die wichtigsten Felder kompakt
    vision_keys = ["absender", "empfaenger", "datum", "betrag", "kennzeichen",
                   "dokumenttyp_visuell", "qr_einzahlungsschein", "sprache", "besonderheiten"]
    vision_kompakt = {k: vision_meta.get(k) for k in vision_keys
                      if vision_meta.get(k) and str(vision_meta.get(k)).lower()
                      not in ("null", "none", "")}

    # Kennzeichen-Override (deterministisch — vor LLM-Entscheidung)
    kennzeichen = vision_meta.get("kennzeichen", "")
    kennzeichen_hinweis = ""
    if kennzeichen and str(kennzeichen).lower() not in ("null", "none", ""):
        nkz = str(kennzeichen).upper().replace(" ", "")
        kz_map = _build_kennzeichen_map()
        if nkz in kz_map:
            fz = kz_map[nkz]
            if fz.get("routing_ordner") and fz.get("ordner"):
                kennzeichen_hinweis = (
                    f"\n>>> KENNZEICHEN {kennzeichen} = ZWINGEND {fz['ordner']} <<<"
                )
            else:
                kennzeichen_hinweis = (
                    f"\n>>> KENNZEICHEN {kennzeichen} bekannt (CF/Person) — kein Ordner-Routing <<<"
                )

    # RAG-Kandidaten (kompakt)
    similar_text = ""
    for i, e in enumerate(similar_entries[:3], 1):  # max 3 statt 5
        pfad   = e.get("pfad", "?")
        erk    = e.get("erkennungsmerkmale", {})
        abgr   = e.get("abgrenzung", "")[:60]
        similar_text += f"  {i}. {pfad} | {erk.get('bereich_absender','')[:50]} | {abgr}\n"

    # Korrekturen (letzte 5)
    recent_corrections = "".join(
        f"  {c.get('vorher','?')} → {c.get('nachher','?')}: {c.get('grund','')}\n"
        for c in corrections[-5:]
    )

    allowed_tags_str  = ", ".join(sorted(allowed_tags))  if allowed_tags  else "(keine)"
    verboten_str      = ", ".join(sorted(verboten_tags)) if verboten_tags else "(keine)"
    typen_str         = ", ".join(sorted(allowed_dokumenttypen)) if allowed_dokumenttypen else "(keine)"

    # OCR auf 2000 Zeichen begrenzen — erste 2000 enthalten immer Absender+Betreff
    ocr_short = ocr_text[:2000]

    # Bekannte Korrespondenten: typische_ordner als starker Hinweis für LLM
    # Funktioniert durch Fuzzy-Match auf Absender-Name aus Vision/OCR
    korr_hinweis = ""
    if corr_map:
        absender = (vision_meta.get("absender") or "").lower().strip()
        for entry in corr_map.get("eintraege", []):
            # Prüfe ob Absender mit diesem Korrespondenten übereinstimmt
            kandidaten = [entry["name"].lower()] +                          [v.lower() for v in entry.get("varianten", [])] +                          [m.lower() for m in entry.get("match", [])]
            if any(k in absender or absender in k for k in kandidaten if len(k) >= 4):
                ordner = entry.get("typische_ordner", [])
                dt = entry.get("default_dokumenttyp", "")
                if ordner:
                    korr_hinweis = (
                        f"\n>>> BEKANNTER KORRESPONDENT: {entry['name']} <<<"
                        f"\n>>> TYPISCHE ORDNER: {', '.join(ordner)} <<<"
                        + (f"\n>>> STANDARD-DOKUMENTTYP: {dt} <<<" if dt else "")
                        + "\n>>> Wähle ZWINGEND einen dieser Ordner ausser du hast starken gegenteiligen Beweis! <<<"
                    )
                break

    # Stufe1-Kontext für Prompt aufbauen
    stufe1_block = ""
    verbotene_doctypen_prompt = []
    verbotene_ordner_prompt   = []
    verbotene_tags_prompt     = []
    nur_doctyp_modus          = False

    if stufe1_kontext:
        nur_doctyp_modus = stufe1_kontext.get("nur_doctyp", False)
        beziehungen_korr = stufe1_kontext.get("beziehungen_korrespondent", [])
        stufe1_grund     = stufe1_kontext.get("stufe1_grund", "")
        verbotene_doctypen_prompt = stufe1_kontext.get("verbotene_doctypen", [])
        verbotene_ordner_prompt   = stufe1_kontext.get("verbotene_ordner", [])
        verbotene_tags_prompt     = stufe1_kontext.get("verbotene_tags", [])
        bevorzugte_ordner         = stufe1_kontext.get("bevorzugte_ordner", [])

        if beziehungen_korr:
            bez_lines = "\n".join(
                f"  - {b.get('person','?')}: {b.get('bezeichnung','?')} "
                f"(Ref: {b.get('referenznummer','—')}) → {b.get('ordner','?')}"
                for b in beziehungen_korr
            )
            stufe1_block = f"\nBEKANNTE BEZIEHUNGEN DIESES KORRESPONDENTEN:\n{bez_lines}\n"
        if stufe1_grund:
            stufe1_block += f"KEIN STUFE-1-MATCH WEIL: {stufe1_grund}\n"
        if bevorzugte_ordner:
            stufe1_block += (
                f"BEVORZUGTE ORDNER (typisch für diesen Korrespondenten, "
                f"nicht zwingend): {', '.join(bevorzugte_ordner)}\n"
            )
        if verbotene_doctypen_prompt:
            stufe1_block += f"VERBOTENE DOKUMENTTYPEN: {', '.join(verbotene_doctypen_prompt)}\n"
        if verbotene_ordner_prompt:
            stufe1_block += f"VERBOTENE ORDNER: {', '.join(verbotene_ordner_prompt)}\n"
        if verbotene_tags_prompt:
            stufe1_block += f"VERBOTENE TAGS (KORRESPONDENT): {', '.join(verbotene_tags_prompt)}\n"

    # Kennzeichen-Hinweise für Prompt dynamisch aus family.json (nur Routing-Einträge)
    kz_map = _build_kennzeichen_map()
    kz_prioritaeten = ""
    i = 0
    for kz_norm, fz in kz_map.items():
        if not fz.get("routing_ordner") or not fz.get("ordner"):
            continue
        i += 1
        kz_prioritaeten += f"{i}. Kennzeichen {fz['kennzeichen_display']} → {fz['ordner']}\n"
    _prio_base = i + 1

    # Beziehungsvorschlag-Aufforderung (nur wenn kein Stufe-1-Match + Korrespondent bekannt)
    beziehung_vorschlag_block = ""
    if stufe1_kontext and stufe1_kontext.get("stufe1_grund") and not nur_doctyp_modus:
        # Valide Werte dynamisch aus JSON-Dateien laden
        _personen_ids = [p.get("id","") for p in _load_family().get("personen", [])]
        _ordner_liste = [e.get("pfad","") for e in manifest if e.get("pfad")]
        _dt_liste     = sorted(constraints.get("allowed_dokumenttypen", set()) or
                               {t["name"] for t in
                                (json.loads(DOCUMENT_TYPES_JSON.read_text(encoding="utf-8"))
                                 .get("typen", []) if DOCUMENT_TYPES_JSON.exists() else [])})

        beziehung_vorschlag_block = (
            f'\nBEZIEHUNGS-VORSCHLAG — falls du Person + Ordner sicher erkennst:\n'
            f'person:           NUR diese IDs: {" | ".join(_personen_ids)}\n'
            f'bezeichnung:      Arzt | Zahnarzt | Arbeitgeber | Bank | Krankenkasse |\n'
            f'                  Versicherung | Steueramt | Stromanbieter | Verein |\n'
            f'                  Abonnement | Sonstiges  (kurz, Deutsch)\n'
            f'referenznummer:   Die eindeutige Nummer im Dokument für diese Person —\n'
            f'                  egal ob Kunden-Nr, Patienten-Nr, Police-Nr, Personal-Nr.\n'
            f'                  Nur den Wert: "19235" | "LV_889.117" | null wenn nicht sichtbar\n'
            f'erlaubte_doctypen: NUR aus: {", ".join(_dt_liste)}\n'
            f'ordner:           NUR aus: {", ".join(_ordner_liste)}\n'
            f'\n"beziehungs_vorschlag":{{"person":"...","bezeichnung":"...",'
            f'"referenznummer":"...|null","erlaubte_doctypen":["..."],"ordner":"..."}}\n'
            f'(weglassen wenn unsicher)\n'
        )

    if nur_doctyp_modus:
        return (
            f"Wähle den passenden Dokumenttyp aus dieser Liste: "
            f"{', '.join(sorted(constraints.get('allowed_dokumenttypen', set())))}\n"
            f"DATEI: {filename}\n"
            f"VISION: {json.dumps(vision_meta, ensure_ascii=False)[:500]}\n"
            f"OCR: {ocr_text[:500]}\n"
            f"{stufe1_block}\n"
            'Antworte NUR mit JSON: {"dokumenttyp_semantisch":"..."}'
        )

    return (
        f"Klassifiziere dieses Dokument für {_get_haushalt_name()} (Schweiz).\n"
        "Antworte NUR mit JSON.\n\n"
        f"DATEI: {filename}\n"
        f"VISION: {json.dumps(vision_kompakt, ensure_ascii=False)}{kennzeichen_hinweis}\n\n"
        f"OCR (erste 2000 Zeichen):\n{ocr_short}\n\n"
        f"{stufe1_block}"
        f"KANDIDATEN (RAG):\n{similar_text or '  (keine)'}\n"
        f"ALLE ORDNER: {', '.join(o for o in ordner_priorisiert if o not in verbotene_ordner_prompt)}\n\n"
        f"ERLAUBTE TAGS (NUR DIESE!): {allowed_tags_str}\n"
        f"VERBOTENE TAGS: {verboten_str}\n"
        f"ERLAUBTE DOKUMENTTYPEN: {typen_str}\n"
        f"MAX TAGS: {max_tags}\n\n"
        f"PRIORITÄTEN:\n"
        + kz_prioritaeten +
        f"{_prio_base}. Versicherungspolice → Familie/Versicherung/Policen\n"
        f"{_prio_base + 1}. Lebensversicherung/Säule3/PK → Familie/Finanzen\n"
        f"{_prio_base + 2}. Reparatur/Service Gerät/Haus → Familie/Haus-Garten\n"
        f"{_prio_base + 3}. SVA/AHV/Gemeinde → Familie/Behörde\n"
        f"{_prio_base + 4}. Steueramt → Person/Steuern\n"
        + (f"\nKORREKTUREN:\n{recent_corrections}" if recent_corrections else "") +
        f"{beziehung_vorschlag_block}"
        f"\nAUSSTELLUNGSDATUM: {DATUM_PROMPT_HINT}\n"
        '\n{"ordner":"...","tags":[...],"korrespondent":"...","titel":"...","datum":"YYYY-MM-DD",'
        '"betrag":"CHF XX.XX","dokumenttyp_semantisch":"...","confidence":"hoch|mittel|tief",'
        '"begruendung":"1 Satz"}'
    )


def sanitize_decision(decision: dict, manifest: list[dict], similar_entries: list[dict]) -> tuple[dict, bool]:
    """
    Sanitization direkt nach LLM-Output + normalize_decision_keys.
    Gibt (sanitized_decision, had_violations) zurück.
    had_violations=True → Confidence wird automatisch reduziert.

    Wichtig: arbeitet auf einer KOPIE — original decision wird nicht mutiert.

    Reihenfolge:
      1. Ordner validieren → Fallback Familie/Sonstiges
      2. Constraints mit bekanntem Zielordner berechnen (folder_tags HART)
      3. Tags filtern (folder_tags hard, tags_global nur Fallback)
      4. Confidence normalisieren
      5. Dokumenttyp validieren
    """
    import copy
    decision = copy.deepcopy(decision)  # KRITISCH 1: nie original mutieren
    # JSON-null vom LLM → leere Strings (sonst .get("key","") liefert None)
    for _k in ("korrespondent", "titel", "dokumenttyp_semantisch", "ordner", "begruendung"):
        if decision.get(_k) is None:
            decision[_k] = ""
    violations = []
    ordner_liste = [e.get("pfad", "?") for e in manifest]

    # 1. Ordner
    if decision.get("ordner") not in ordner_liste:
        violations.append(f"ungültiger Ordner: {decision.get('ordner')!r}")
        decision["ordner"] = "Familie/Sonstiges"

    # 2. Constraints mit finalem Ordner
    constraints = build_constraints(similar_entries, target_ordner=decision["ordner"])
    allowed_tags          = constraints["allowed_tags"]
    verboten_tags         = constraints["verboten_tags"]
    allowed_dokumenttypen = constraints["allowed_dokumenttypen"]
    max_tags              = constraints["max_tags"]

    # Tags: Suggest-Modus statt Hard-Allow
    # erlaubte_tags im Manifest sind VORSCHLÄGE — kein hartes Verbot
    # Nur verbotene_tags werden wirklich entfernt (Jahreszahlen, Monatsnamen etc.)
    _ordner_entry = next((e for e in manifest if e.get("pfad") == decision["ordner"]), {})
    _vorgeschlagene_tags = set(_ordner_entry.get("erlaubte_tags", []))

    # Globale verbotene Tags aus Manifest-Root laden
    _global_verboten = set()
    try:
        manifest_root = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        _global_verboten.update(manifest_root.get("verbotene_tags_global", []))
    except Exception:
        pass
    _alle_verboten = verboten_tags | _global_verboten

    # 3. Tags
    raw_tags = decision.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    elif not isinstance(raw_tags, list):
        raw_tags = []

    # Nur verbotene Tags entfernen — erlaubte Tags sind nur Hinweise
    clean_tags = [t for t in raw_tags if t not in _alle_verboten]
    removed = set(raw_tags) - set(clean_tags)
    if removed:
        # Triviale Tags (z.B. Jahreszahlen) nicht als echte Violation zählen
        non_trivial_removed = {t for t in removed if not _is_trivial_tag_violation(t)}
        if non_trivial_removed:
            violations.append(f"Tags verworfen (verboten): {non_trivial_removed}")
            log.warning("Sanitize: Tags verworfen (non-trivial): %s", non_trivial_removed)
        if removed - non_trivial_removed:
            log.info("Sanitize: Tags verworfen (trivial, kein Downgrade): %s", removed - non_trivial_removed)

    # Tag-Ausschluss via tags.json
    _pseudo_ocr    = " ".join(filter(None, [str(decision.get("korrespondent") or ""),
                                          str(decision.get("titel") or ""),
                                          str(decision.get("dokumenttyp_semantisch") or "")]))
    _pseudo_vision = {"absender": str(decision.get("korrespondent") or ""),
                      "dokumenttyp_visuell": str(decision.get("dokumenttyp_semantisch") or "")}
    clean_tags = _filter_excluded_tags(clean_tags, _pseudo_ocr, _pseudo_vision)

    # Confidence-Downgrade wenn Tags ausserhalb der Vorschläge
    if _vorgeschlagene_tags:
        unbekannte = set(clean_tags) - _vorgeschlagene_tags
        if unbekannte:
            log.info("Sanitize: Tags ausserhalb Vorschläge (erlaubt): %s", unbekannte)
            # Kein Downgrade mehr — nur Info-Log

    decision["tags"] = clean_tags[:max_tags]

    # 4. Confidence normalisieren
    conf_raw = decision.get("confidence", "tief")
    if isinstance(conf_raw, (int, float)):
        conf_raw = "hoch" if conf_raw >= 0.8 else "mittel" if conf_raw >= 0.5 else "tief"
    confidence = str(conf_raw).strip().lower()
    if confidence not in {"hoch", "mittel", "tief"}:
        violations.append(f"ungültige Confidence: {confidence!r}")
        confidence = "tief"
    decision["confidence"] = confidence

    # 5. Dokumenttyp
    dt = (decision.get("dokumenttyp_semantisch") or "").strip()
    if allowed_dokumenttypen and dt and dt not in allowed_dokumenttypen:
        # Prüfen ob Typ global bekannt (in document_types.json)
        _known_types = set()
        try:
            if DOCUMENT_TYPES_JSON.exists():
                _dt_data = json.loads(DOCUMENT_TYPES_JSON.read_text(encoding="utf-8"))
                for _t in _dt_data.get("typen", []):
                    _known_types.add(_t["name"].lower())
                    for _s in _t.get("synonyme", []):
                        _known_types.add(_s.lower())
        except Exception:
            pass

        if dt.lower() in _known_types:
            # Typ ist global bekannt — nur nicht für diesen Ordner konfiguriert.
            # Typ BEHALTEN, aber als Violation loggen (senkt Confidence)
            # UND Manifest-Lernvorschlag: Typ zum Ordner hinzufügen
            violations.append(f"Dokumenttyp '{dt}' nicht für Ordner '{decision.get('ordner')}' konfiguriert — Manifest aktualisiert")
            log.info("Sanitize: Dokumenttyp '%s' bekannt aber nicht im Manifest für '%s' → behalten + Manifest-Update",
                     dt, decision.get("ordner"))
            # Manifest-Lernvorschlag: Typ zum Ordner ergänzen
            try:
                _manifest_path = MANIFEST_PATH
                if _manifest_path.exists():
                    _mdata = json.loads(_manifest_path.read_text(encoding="utf-8"))
                    for _entry in _mdata.get("ordner", []):
                        if _entry.get("pfad") == decision.get("ordner"):
                            _existing = _entry.get("erlaubte_dokumenttypen", [])
                            if dt not in _existing:
                                _existing.append(dt)
                                _entry["erlaubte_dokumenttypen"] = _existing
                                _manifest_path.write_text(
                                    json.dumps(_mdata, ensure_ascii=False, indent=2),
                                    encoding="utf-8"
                                )
                                log.info("Manifest-Lernvorschlag: '%s' zu Ordner '%s' hinzugefügt",
                                         dt, decision.get("ordner"))
                            break
            except Exception as _me:
                log.warning("Manifest-Update fehlgeschlagen: %s", _me)
        else:
            # Typ komplett unbekannt → sinnvollster Fallback ist leer (kein Typ)
            # statt alphabetisch erstem Typ
            violations.append(f"Dokumenttyp '{dt}' unbekannt — kein Fallback gesetzt")
            log.warning("Sanitize: Dokumenttyp '%s' unbekannt — wird nicht gesetzt", dt)
            decision["dokumenttyp_semantisch"] = ""

    # 5b. Ausschluss-Check: Dokumenttyp via Ausschluss-Keywords verwerfen
    # Läuft NACH Constraint-Check — verhindert dass ausgeschlossener Typ gesetzt wird
    # ocr_text und vision_meta werden als module-level Kontext nicht übergeben →
    # Ausschluss-Check hier nur auf Typ-Name-Basis (Vollcheck in resolve_document_type)
    dt_final = (decision.get("dokumenttyp_semantisch") or "").strip()
    if dt_final:
        _load_ausschluss_map()
        # Kontext aus decision (kein ocr/vision hier) — nutze verfügbare Felder
        _pseudo_ocr  = " ".join(filter(None, [str(decision.get("korrespondent") or ""),
                                              str(decision.get("titel") or "")]))
        _pseudo_vision = {"absender": str(decision.get("korrespondent") or ""),
                          "dokumenttyp_visuell": str(decision.get("dokumenttyp_semantisch") or "")}
        if _doctype_is_excluded(dt_final, _pseudo_ocr, _pseudo_vision):
            violations.append(f"Dokumenttyp '{dt_final}' durch Ausschluss-Keyword verworfen")
            log.warning("Sanitize: Dokumenttyp '%s' via Ausschluss-Check entfernt", dt_final)
            decision["dokumenttyp_semantisch"] = ""

    # Confidence automatisch reduzieren bei Violations
    if violations and decision["confidence"] == "hoch":
        decision["confidence"] = "mittel"
        log.info("Sanitize: Confidence hoch→mittel wegen Violations: %s", violations)
    elif len(violations) >= 2 and decision["confidence"] == "mittel":
        decision["confidence"] = "tief"
        log.info("Sanitize: Confidence mittel→tief wegen %d Violations", len(violations))

    had_violations = bool(violations)
    if had_violations:
        log.info("Sanitize: %d Violations korrigiert: %s", len(violations), violations)
    else:
        log.info("Sanitize: OK (kein Violation)")

    return decision, had_violations


LLM_SYSTEM = "Du bist ein präziser Dokumenten-Klassifikator. Antworte ausschliesslich mit einem validen JSON-Objekt. Kein Markdown, keine Erklärung."


def llm_decide(prompt: str) -> dict:
    try:
        resp = ollama_post(
            "api/chat",
            {
                "model": MODEL_LLM,
                "messages": [{"role": "user", "content": prompt}],
                "system": LLM_SYSTEM,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.05, "num_predict": 256},
            },
            timeout=LLM_TIMEOUT,
        )
        raw = resp.get("message", {}).get("content", "")
        return extract_json_from_response(raw)
    except Exception as e:
        log.error("LLM Aufruf fehlgeschlagen: %s", e)
        return {}


# ─── Paperless API: Objekte auflösen/anlegen ──────────────────────────────────

def _get_by_name(endpoint: str, name: str) -> Optional[int]:
    r = _http.get(
        f"{PAPERLESS_URL}/api/{endpoint}/",
        params={"name__iexact": name},
        headers=_headers(),
        timeout=15,
    )
    if not r.ok:
        log.warning("GET %s '%s' → HTTP %s: %s", endpoint, name, r.status_code, r.text[:100])
        return None
    try:
        data = r.json()
    except Exception:
        log.warning("GET %s '%s' — kein JSON (Token falsch?): %s", endpoint, name, r.text[:100])
        return None
    for item in data.get("results", []):
        if item["name"].lower() == name.lower():
            return item["id"]
    return None


# Gruppen-IDs für Permissions — aus Env oder Defaults
# Müssen mit den Paperless-Gruppen "Familie" (view) und "Eltern" (change) übereinstimmen
_PERM_VIEW_GROUPS   = [int(g) for g in os.environ.get("PAPERLESS_VIEW_GROUP_IDS",  "1").split(",") if g.strip().isdigit()]
_PERM_CHANGE_GROUPS = [int(g) for g in os.environ.get("PAPERLESS_CHANGE_GROUP_IDS", "2").split(",") if g.strip().isdigit()]


def _default_permissions() -> dict:
    """Zentrale Permissions-Payload — wird überall verwendet.
    Einzige Stelle wo Gruppen-IDs gesetzt werden.
    """
    return {
        "set_permissions": {
            "view":   {"users": [], "groups": _PERM_VIEW_GROUPS},
            "change": {"users": [], "groups": _PERM_CHANGE_GROUPS},
        },
    }


def _create_obj(endpoint: str, name: str, extra: dict = None) -> Optional[int]:
    """Objekt anlegen mit korrekten Gruppen-Permissions.
    Ohne Permissions sind neu angelegte Tags/Typen/Pfade für andere User nicht sichtbar
    und erscheinen als 'Private' in der Paperless-UI.
    """
    payload = {"name": name}
    if extra:
        payload.update(extra)
    # Zentrale Permissions — verhindert "Private"-Anzeige für andere User
    payload.update(_default_permissions())
    r = _http.post(
        f"{PAPERLESS_URL}/api/{endpoint}/",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    if r.ok:
        obj_id = r.json().get("id")
        log.info("Angelegt: %s '%s' → ID=%s", endpoint, name, obj_id)
        # Paperless ignoriert set_permissions beim POST für manche Objekte
        # → sofort PATCH um Permissions sicherzustellen
        if obj_id:
            _http.patch(
                f"{PAPERLESS_URL}/api/{endpoint}/{obj_id}/",
                json=_default_permissions(),
                headers=_headers(),
                timeout=10,
            )
        return obj_id
    log.warning("POST %s '%s' fehlgeschlagen: %s", endpoint, name, r.text[:100])
    return _get_by_name(endpoint, name)


# Cache für bekannte Tags: name.lower() → id
_KNOWN_TAGS_CACHE: dict[str, Optional[int]] = {}
_KNOWN_TAGS_LOADED: bool = False


def _load_known_tags() -> None:
    """Alle bekannten Tags aus Paperless laden und cachen.
    Wird einmalig beim ersten Aufruf geladen.
    """
    global _KNOWN_TAGS_LOADED
    if _KNOWN_TAGS_LOADED:
        return
    try:
        result = _http.get(
            f"{PAPERLESS_URL}/api/tags/?page_size=500",
            headers=_headers(), timeout=15
        ).json()
        for tag in result.get("results", []):
            _KNOWN_TAGS_CACHE[tag["name"].lower()] = tag["id"]
        _KNOWN_TAGS_LOADED = True
        log.info("Tag-Cache geladen: %d bekannte Tags", len(_KNOWN_TAGS_CACHE))
    except Exception as e:
        log.warning("Tag-Cache laden fehlgeschlagen: %s", e)


def _is_legacy_consume_path(path: str) -> bool:
    """True wenn Pfad LEGACY_CONSUME_MARKERS enthält (pre_consume: consume/legacy/…)."""
    markers = [
        m.strip() for m in os.environ.get("LEGACY_CONSUME_MARKERS", "/legacy/").split(",")
        if m.strip()
    ]
    if not markers or not path:
        return False
    p = path.replace("\\", "/").lower()
    return any(m.lower() in p for m in markers)


LEGACY_MARKER_DIR = Path(
    os.environ.get("LEGACY_MARKER_DIR", "/tmp/paperless_legacy_markers")
)
LEGACY_SET_BATCH_TAG = os.environ.get("LEGACY_SET_BATCH_TAG", "false").lower() in (
    "1", "true", "yes",
)
LEGACY_STORAGE_PATH_NAME = os.environ.get("LEGACY_STORAGE_PATH_NAME", "Legacy")
LEGACY_STORAGE_PATH_TEMPLATE = os.environ.get("LEGACY_STORAGE_PATH_TEMPLATE", "legacy/{title}")


def _has_legacy_tags_in_env() -> bool:
    """Sidecar-Tags sind beim post_consume bereits in DOCUMENT_TAGS (vor Pipeline)."""
    tags_raw = os.environ.get("DOCUMENT_TAGS", "")
    if not tags_raw.strip():
        return False
    legacy_tag = os.environ.get("LEGACY_TAG", "legacy").lower()
    for t in tags_raw.split(","):
        tl = t.strip().lower()
        if not tl:
            continue
        if tl == legacy_tag or tl.startswith("legacy-"):
            return True
    return False


def _take_legacy_marker_path() -> str:
    """Consume-Pfad aus pre_consume-Marker (einmal lesen + löschen)."""
    orig = os.environ.get("DOCUMENT_ORIGINAL_FILENAME", "").strip()
    for name in (orig, DOCUMENT_FILE_NAME):
        if not name:
            continue
        marker = LEGACY_MARKER_DIR / Path(name).name
        if not marker.is_file():
            continue
        try:
            stored = marker.read_text(encoding="utf-8").strip()
            marker.unlink(missing_ok=True)
            return stored
        except OSError as e:
            log.warning("Legacy-Marker lesen fehlgeschlagen (%s): %s", marker, e)
    return ""


def _legacy_batch_tag_from_path(path: str) -> Optional[str]:
    """consume/legacy/moni-2015-test/… → legacy-moni-2015-test"""
    if not path:
        return None
    markers = [
        m.strip() for m in os.environ.get("LEGACY_CONSUME_MARKERS", "/legacy/").split(",")
        if m.strip()
    ]
    norm = path.replace("\\", "/")
    for m in markers:
        idx = norm.lower().find(m.lower())
        if idx < 0:
            continue
        tail = norm[idx + len(m):].lstrip("/")
        if not tail:
            return None
        return f"legacy-{tail.split('/')[0]}"
    return None


def _is_legacy_import(marker_path: str = "") -> bool:
    """Legacy-Altbestand — pre_consume-Pfad, Sidecar-Tags oder Marker."""
    if _is_legacy_consume_path(DOCUMENT_SOURCE_PATH):
        return True
    if _has_legacy_tags_in_env():
        return True
    if marker_path and _is_legacy_consume_path(marker_path):
        return True
    return False


def _finalize_legacy_import(document_id: int, consume_path: str = "") -> None:
    """Altbestand: Tag legacy (+ optional Batch), Speicherpfad legacy/{title}."""
    tag_names = [os.environ.get("LEGACY_TAG", "legacy")]
    if LEGACY_SET_BATCH_TAG:
        batch_tag = _legacy_batch_tag_from_path(consume_path)
        if batch_tag:
            tag_names.append(batch_tag)
    patch: dict = {}
    try:
        doc = _http.get(
            _api_url(f"documents/{document_id}/"), headers=_headers(), timeout=30
        ).json()
        existing_tags = list(doc.get("tags") or [])
        to_add: list[int] = []
        for name in tag_names:
            tid = resolve_tag(name)
            if not tid:
                if name == os.environ.get("LEGACY_TAG", "legacy"):
                    log.warning(
                        "Legacy-Tag '%s' fehlt in Paperless — bitte in Admin anlegen", name
                    )
                continue
            if tid not in existing_tags and tid not in to_add:
                to_add.append(tid)
        if to_add:
            patch["tags"] = existing_tags + to_add

        if STORAGE_MODE == "api":
            sp_id = resolve_storage_path_with_template(
                LEGACY_STORAGE_PATH_NAME, LEGACY_STORAGE_PATH_TEMPLATE
            )
            if sp_id and doc.get("storage_path") != sp_id:
                patch["storage_path"] = sp_id
            elif not sp_id:
                log.warning(
                    "Legacy-Speicherpfad '%s' nicht anlegbar", LEGACY_STORAGE_PATH_NAME
                )

        if not patch:
            log.info("Legacy-Import #%s — bereits korrekt getaggt/gespeichert", document_id)
            return
        _http.patch(
            _api_url(f"documents/{document_id}/"),
            headers=_headers(),
            json=patch,
            timeout=30,
        ).raise_for_status()
        log.info(
            "Legacy-Import #%s — %s",
            document_id,
            ", ".join(
                x for x in (
                    f"Tags: {tag_names}" if to_add else "",
                    f"Speicherpfad: {LEGACY_STORAGE_PATH_TEMPLATE}"
                    if patch.get("storage_path") else "",
                ) if x
            ),
        )
    except Exception as e:
        log.warning("Legacy-Finalize für #%s fehlgeschlagen: %s", document_id, e)


def resolve_tag(name: str) -> Optional[int]:
    """Tag-ID nachschlagen — NUR bestehende Tags.
    Neue Tags werden NICHT angelegt (nur menschliche Pflege erlaubt).
    Unbekannte Tags werden still ignoriert.
    """
    if not name or name.lower() in ("null", "none", ""):
        return None
    _load_known_tags()
    tag_id = _KNOWN_TAGS_CACHE.get(name.lower())
    if tag_id is None:
        log.info("Tag '%s' unbekannt — wird ignoriert (nur menschliche Pflege)", name)
    return tag_id


def resolve_correspondent(name: str) -> Optional[int]:
    if not name or name.lower() in ("null", "none"):
        return None
    try:
        return _get_by_name("correspondents", name) or _create_obj("correspondents", name)
    except Exception as e:
        log.warning("Korrespondent '%s': %s", name, e)
        return None



# ── Korrespondenten-Kanonisierung ────────────────────────────────────────────
# Benötigt: rapidfuzz (pip install rapidfuzz)
# Dateien:  training/correspondents.json
#           training/pending_correspondents.jsonl

CORRESPONDENTS_PATH = Path(os.environ.get(
    "CORRESPONDENTS_JSON",
    "/opt/paperless-scripts/training/correspondents.json"
))
PENDING_CORR_PATH = Path(os.environ.get(
    "PENDING_JSONL",
    "/opt/paperless-scripts/training/pending_correspondents.jsonl"
))
PENDING_BEZIEHUNGEN_PATH = Path(os.environ.get(
    "PENDING_BEZIEHUNGEN_JSONL",
    "/opt/paperless-scripts/training/pending_beziehungen.jsonl"
))
BRILLENPAESSE_PATH = Path(os.environ.get(
    "BRILLENPAESSE_JSON",
    "/opt/paperless-scripts/training/brillenpaesse.json",
))
PENDING_BRILLENPASS_PATH = Path(os.environ.get(
    "PENDING_BRILLENPASS_JSONL",
    "/opt/paperless-scripts/training/pending_brillenpass.jsonl",
))
PENDING_REVIEW_TAG       = os.environ.get("PENDING_REVIEW_TAG",       "pending_review")
PENDING_QS_TAG           = os.environ.get("PENDING_QS_TAG",           "pending_qs")
PENDING_NEW_CORR_TAG     = os.environ.get("PENDING_NEW_CORR_TAG",      "pending_new_correspondent")
PENDING_BRILLENPASS_TAG  = os.environ.get("PENDING_BRILLENPASS_TAG",  "pending_brillenpass")
PENDING_HTR_DECISION_TAG = os.environ.get("PENDING_HTR_DECISION_TAG", "pending_htr_decision")
PENDING_HTR_DECISION_PATH = Path(os.environ.get(
    "PENDING_HTR_DECISION_JSONL",
    "/opt/paperless-scripts/training/pending_htr_decision.jsonl",
))

# Alle System-Pending-Tags (für cleanup beim Freigeben)
ALL_PENDING_TAGS = {
    PENDING_REVIEW_TAG, PENDING_QS_TAG, PENDING_NEW_CORR_TAG,
    PENDING_BRILLENPASS_TAG, PENDING_HTR_DECISION_TAG,
}
HIGH_THRESHOLD  = int(os.environ.get("HIGH_THRESHOLD",  "90"))  # ergaenzung
MERGE_THRESHOLD = int(os.environ.get("MERGE_THRESHOLD", "80"))  # merge_into — höher = weniger False Positives

# PENDING_MODE: steuert wann pending_review Tag gesetzt wird
# "always"    → alle Dokumente bekommen pending_qs (QS-Modus)
# "uncertain" → nur bei unbekanntem Korrespondent, tiefer Confidence oder Fallback (Standard)
# "never"     → nie pending (nur für Tests)
PENDING_MODE = os.environ.get("PENDING_MODE", "uncertain")


def _normalize_corr(text: str) -> str:
    """Lowercase, Umlaute erhalten, Mehrfach-Whitespace zusammenführen."""
    import unicodedata
    text = text.lower().strip()
    text = unicodedata.normalize("NFC", text)
    import re as _re
    text = _re.sub(r"\s+", " ", text)
    return text


def _load_corr_map() -> dict:
    """correspondents.json laden (shared-read, kein Lock nötig)."""
    if not CORRESPONDENTS_PATH.exists():
        return {"version": "1.0", "eintraege": []}
    with open(CORRESPONDENTS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_corr_ids_only(corr_map: dict) -> None:
    """correspondents.json atomar mit flock schreiben."""
    CORRESPONDENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Datei anlegen falls nicht vorhanden
    if not CORRESPONDENTS_PATH.exists():
        CORRESPONDENTS_PATH.write_text(
            json.dumps({"version": "1.0", "eintraege": []}, ensure_ascii=False, indent=2)
        )
    fd = open(CORRESPONDENTS_PATH, "r+", encoding="utf-8")
    try:
        import fcntl as _fcntl
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        # Nochmals lesen (anderer Prozess könnte inzwischen geschrieben haben)
        fd.seek(0)
        current = json.load(fd)
        # Nur _paperless.id Felder mergen — keine anderen Änderungen überschreiben
        current_by_name = {e["name"]: e for e in current.get("eintraege", [])}
        for entry in corr_map.get("eintraege", []):
            name = entry["name"]
            if name in current_by_name:
                pid = entry.get("_paperless", {}).get("id")
                if pid:
                    current_by_name[name].setdefault("_paperless", {})["id"] = pid
        fd.seek(0)
        fd.truncate()
        json.dump(current, fd, ensure_ascii=False, indent=2)
        fd.flush()
    finally:
        import fcntl as _fcntl
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        fd.close()


def _find_exact_match(corr_map: dict, raw_name: str) -> Optional[dict]:
    """Korrespondent via _resolve_corr_entry (exakt → Substring → Overlap)."""
    return _resolve_corr_entry(corr_map, raw_name)


# Firmennamen-Suffixe die beim Fuzzy-Match normalisiert werden sollen
# "Zurich Versicherung AG" und "Zürich Versicherungs-Gesellschaft AG" → beide "zurich versicherung"
_FIRMA_SUFFIXE = [
    r'\s+ag$', r'\s+gmbh$', r'\s+sa$', r'\s+sarl$', r'\s+ag\s+co\.?\s+kg$',
    r'\s+gesellschaft$', r'\s+-gesellschaft$', r'\s+versicherungs-gesellschaft$',
    r'\s+versicherung$', r'\s+versicherungen$', r'\s+bank$', r'\s+gruppe$',
    r'\s+holding$', r'\s+stiftung$', r'\s+genossenschaft$',
]
# Kritische Namen die NICHT automatisch gemergt werden dürfen
# (zu ähnliche Namen aber verschiedene Entitäten)
# Fallback-Paare wenn nicht_verwechseln_mit in correspondents.json nicht gepflegt ist
_FUZZY_BLACKLIST_PAIRS = [
    ("zurich versicherung", "zurich lebensversicherung"),
    ("css versicherung", "css krankenversicherung"),
    ("helvetia versicherung", "helvetia leben"),
    ("axa versicherung", "axa leben"),
]


def _normalize_firma(text: str) -> str:
    """Firmennamen-Suffixe entfernen + Umlaut-Normalisierung für Fuzzy-Vergleich."""
    import re as _re
    t = _normalize_corr(text)
    # Umlaut-Varianten angleichen: ü↔ue, ä↔ae, ö↔oe
    t = t.replace("ü", "u").replace("ue", "u")
    t = t.replace("ä", "a").replace("ae", "a")
    t = t.replace("ö", "o").replace("oe", "o")
    # Bindestriche normalisieren
    t = t.replace("-", " ").replace("  ", " ")
    # Firmen-Suffixe entfernen
    for pattern in _FIRMA_SUFFIXE:
        t = _re.sub(pattern, "", t, flags=_re.IGNORECASE).strip()
    return t.strip()


# Wörter die beim Token-Overlap NICHT zählen (zu generisch/geografisch)
_TOKEN_OVERLAP_STOPWORDS = {
    # Kantone + Städte
    "aargau", "zurich", "zürich", "bern", "basel", "luzern", "zürich",
    "winterthur", "biel", "thun", "solothurn", "zug", "schaffhausen",
    "frauenfeld", "aarau", "liestal", "herisau", "stans", "appenzell",
    "glarus", "schwyz", "altdorf", "sarnen", "bellinzona", "sion", "chur",
    "lausanne", "genf", "genève", "lugano", "st.gallen", "gallen",
    # Geografische Zusätze
    "schweiz", "swiss", "suisse", "svizzera",
    # Generische Begriffe
    "ag", "gmbh", "sa", "sarl", "ltd",
    "nord", "sud", "ost", "west", "zentral", "mittel",
    "region", "regional",
    # Behörden-Boilerplate (sonst Steueramt ↔ Strassenverkehrsamt via «des Kantons»)
    "des", "der", "die", "das", "dem", "den", "vom", "von", "bei", "mit", "und",
    "kantons", "kanton", "departement", "volkswirtschaft", "inneres",
}


def _token_overlap(a: str, b: str) -> float:
    """Token-Overlap-Score: Anteil gemeinsamer Tokens. 0.0–1.0.
    Geografische und generische Wörter werden ignoriert.
    """
    tokens_a = {t for t in a.split() if t not in _TOKEN_OVERLAP_STOPWORDS}
    tokens_b = {t for t in b.split() if t not in _TOKEN_OVERLAP_STOPWORDS}
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = tokens_a & tokens_b
    return len(overlap) / max(len(tokens_a), len(tokens_b))


def _is_blacklisted_pair(a: str, b: str) -> bool:
    """Prüft ob zwei normalisierte Namen auf der Blacklist stehen."""
    na, nb = _normalize_firma(a), _normalize_firma(b)
    for p1, p2 in _FUZZY_BLACKLIST_PAIRS:
        if (p1 in na and p2 in nb) or (p2 in na and p1 in nb):
            return True
        if (p1 in nb and p2 in na) or (p2 in nb and p1 in na):
            return True
    return False


def _distinctive_token_shared(a: str, b: str, min_len: int = 6) -> bool:
    """Gemeinsames Wort ≥ min_len — z. B. «strassenverkehrsamt» in Vision + langem Behördennamen."""
    words_a = {w for w in _normalize_firma(a).split() if len(w) >= min_len}
    words_b = {w for w in _normalize_firma(b).split() if len(w) >= min_len}
    return bool(words_a & words_b)


def _name_hits(a: str, b: str) -> bool:
    """True wenn a und b gleich/normalisiert gleich oder einer im anderen enthalten ist."""
    if not a or not b:
        return False
    a_l, b_l = a.lower().strip(), b.lower().strip()
    if _normalize_corr(a) == _normalize_corr(b):
        return True
    return b_l in a_l or a_l in b_l


def _is_merge_forbidden(corr_map: dict, raw_name: str, target_entry: dict) -> bool:
    """Fuzzy-Merge raw_name → target_entry verboten (nicht_verwechseln_mit + Fallback-Blacklist)."""
    target_name = target_entry.get("name", "")
    if not target_name:
        return False
    if _is_blacklisted_pair(raw_name, target_name):
        return True
    for nv in target_entry.get("nicht_verwechseln_mit", []):
        if _name_hits(raw_name, nv):
            return True
    source_entry = _resolve_corr_entry(corr_map, raw_name)
    if source_entry:
        for nv in source_entry.get("nicht_verwechseln_mit", []):
            if _name_hits(target_name, nv):
                return True
    for entry in corr_map.get("eintraege", []):
        for nv in entry.get("nicht_verwechseln_mit", []):
            if not _name_hits(target_name, nv):
                continue
            if source_entry and source_entry.get("name") == entry.get("name"):
                return True
            for k in _corr_kandidaten_strings(entry):
                if k and _name_hits(raw_name, k):
                    return True
    return False


def _find_fuzzy_match(corr_map: dict, raw_name: str) -> tuple[Optional[dict], int]:
    """
    Fuzzy-Match via rapidfuzz gegen alle name + varianten.
    Sicherheitsmassnahmen:
      - Firmen-Suffix-Normalisierung (AG/GmbH/Versicherung etc.)
      - Umlaut-Angleichung (Zurich/Zürich)
      - Token-Overlap als Zusatzscore (verhindert falsche Merges bei kurzen Matches)
      - nicht_verwechseln_mit aus correspondents.json (paper.manager)
      - Fallback-Blacklist für bekannte Versicherungs-Paare
    Gibt (bester_Eintrag, Score) zurück. Score 0-100.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        log.warning("rapidfuzz nicht installiert — Fuzzy-Match übersprungen")
        return None, 0

    norm      = _normalize_corr(raw_name)
    norm_firm = _normalize_firma(raw_name)
    best_entry = None
    best_score = 0

    for entry in corr_map.get("eintraege", []):
        if _is_corr_platzhalter(entry):
            continue
        if _is_merge_forbidden(corr_map, raw_name, entry):
            log.info("Fuzzy: Merge verboten: '%s' ↛ '%s'",
                     raw_name, entry.get("name", "?"))
            continue
        candidates = [entry["name"]] + entry.get("varianten", [])
        for candidate in candidates:
            cand_norm  = _normalize_corr(candidate)
            cand_firm  = _normalize_firma(candidate)

            # Score 1: WRatio auf normalisierten Namen
            score_wratio = fuzz.WRatio(cand_norm, norm)
            # Score 2: WRatio auf Firma-normalisierten Namen (ohne Suffixe)
            score_firma  = fuzz.WRatio(cand_firm, norm_firm) if cand_firm and norm_firm else 0
            # Score 3: Token-Overlap (0–100)
            score_token  = int(_token_overlap(cand_firm, norm_firm) * 100)

            # Kombinierter Score: WRatio dominiert, Token-Overlap als Tiebreaker
            # Firma-Score erhöht Score wenn Suffixe den WRatio verwässern
            combined = max(score_wratio, score_firma) * 0.8 + score_token * 0.2

            # Sicherheitsschwelle: Token-Overlap < 0.3 bei Score ≥ 70 → verdächtig
            # Ausnahme: gemeinsames Unterscheidungswort (z. B. strassenverkehrsamt)
            if combined >= 70 and score_token < 30 and not _distinctive_token_shared(raw_name, candidate):
                log.info(
                    "Fuzzy: Score %d aber Token-Overlap nur %d%% für '%s' ↔ '%s' — reduziert",
                    combined, score_token, raw_name, candidate
                )
                combined = min(combined, 65)  # unter MERGE_THRESHOLD drücken

            if combined > best_score:
                best_score = int(combined)
                best_entry = entry

    if best_entry:
        log.info("Fuzzy-Match: '%s' → '%s' (Score %d)", raw_name, best_entry["name"], best_score)
    return best_entry, best_score


def _append_pending_corr(entry: dict) -> None:
    """Thread-/Prozess-sicher in pending_correspondents.jsonl schreiben."""
    PENDING_CORR_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PENDING_CORR_PATH, "a", encoding="utf-8") as f:
        import fcntl as _fcntl
        _fcntl.flock(f, _fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        finally:
            _fcntl.flock(f, _fcntl.LOCK_UN)
    log.info("Pending-Korrespondent: aktion=%s name=%s",
             entry.get("aktion"), entry.get("vorgeschlagener_eintrag", {}).get("name", "?"))


def write_pending_beziehung(vorschlag: dict, document_id: int, korrespondent_name: str) -> None:
    """Beziehungs-Vorschlag in pending_beziehungen.jsonl schreiben."""
    import time as _time
    import fcntl as _fcntl
    entry = {
        "status":           "pending",
        "timestamp":        _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "document_id":      document_id,
        "korrespondent":    korrespondent_name,
        "person":           vorschlag.get("person", ""),
        "bezeichnung":      vorschlag.get("bezeichnung", ""),
        "referenznummer":   vorschlag.get("referenznummer", ""),
        "erlaubte_doctypen": vorschlag.get("erlaubte_doctypen", []),
        "ordner":           vorschlag.get("ordner", ""),
    }
    PENDING_BEZIEHUNGEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PENDING_BEZIEHUNGEN_PATH, "a", encoding="utf-8") as f:
        _fcntl.flock(f, _fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        finally:
            _fcntl.flock(f, _fcntl.LOCK_UN)
    log.info("Pending-Beziehung: korrespondent=%s person=%s ordner=%s",
             korrespondent_name, entry["person"], entry["ordner"])


def _build_pending_entry(aktion: str, raw_name: str, document_id: int,
                          fuzzy_match: Optional[dict] = None,
                          fuzzy_score: int = 0,
                          identifikatoren: dict | None = None) -> dict:
    """Pending-Eintrag im Schema von pending_correspondents.jsonl aufbauen."""
    import time as _time
    idents = identifikatoren or {"uid": [], "iban": [], "email": [], "telefon": []}
    vorschlag = {
        "name": raw_name,
        "varianten": [],
        "match": [_normalize_corr(raw_name)],
        "matching_algorithm": "any",
        "typ": "",
        "typische_ordner": [],
        "notiz": "",
        "identifikatoren": idents,
        "_paperless": {"id": None, "is_insensitive": True, "owner": None,
                       "permissions": {"view": {"users": [], "groups": []},
                                       "change": {"users": [], "groups": []}}},
    }
    entry = {
        "aktion":       aktion,
        "status":       "pending",
        "pending_type": PENDING_NEW_CORR_TAG,   # immer neuer Korrespondent
        "llm_raw":      raw_name,
        "llm_confidence": 1.0,
        "source_document_ids": [document_id],
        "created_at":   _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reviewed_by":  None,
        "reviewed_at":  None,
        "vorgeschlagener_eintrag": vorschlag,
    }
    if aktion == "ergaenzung" and fuzzy_match:
        entry["ziel_name"]      = fuzzy_match["name"]
        entry["fuzzy_score"]    = fuzzy_score
        entry["merge_hinweis"]  = f"Fuzzy-Score {fuzzy_score}% — mögliche Ergänzung zu '{fuzzy_match['name']}'"
    elif aktion == "merge_into" and fuzzy_match:
        entry["merge_ziel_name"]   = fuzzy_match["name"]
        entry["fuzzy_score"]       = fuzzy_score
        entry["merge_begruendung"] = f"Fuzzy-Score {fuzzy_score}% — vermutlich identisch mit '{fuzzy_match['name']}'"
    return entry


def resolve_correspondent_canonical(
    raw_name: str,
    document_id: int,
    *,
    ocr_text: str = "",
    qr_meta: dict | None = None,
    vision_meta: dict | None = None,
) -> tuple[Optional[int], bool, Optional[str]]:
    """
    Hauptfunktion: Kanonisierung + Paperless-Zuordnung.

    Ablauf:
      1. Match via _resolve_corr_entry (exakt → Substring → Overlap) → Paperless-ID
      2. Kein Match → Fuzzy nur für Pending-Vorschlag (ergaenzung / merge_into / neu)
         ≥ 90% → "ergaenzung" in pending
         ≥ 70% → "merge_into" in pending
         <  70% → "neu" in pending

    Gibt (korr_id_oder_None, pending_review_needed, default_dokumenttyp_oder_None) zurück.
    pending_review_needed=True → Tag im finalen PATCH hinzufügen.
    default_dokumenttyp → wird als Hinweis in sanitize_decision verwendet.
    """
    if not raw_name or raw_name.lower() in ("null", "none", ""):
        return None, False, None

    corr_map = _load_corr_map()

    # ── Schritt 1: Deterministischer Match (match[] / varianten / Substring) ───
    matched = _resolve_corr_entry(corr_map, raw_name)
    if matched:
        paperless_id = matched.get("_paperless", {}).get("id")
        default_dt = matched.get("default_dokumenttyp") or matched.get("typ") or None
        if paperless_id:
            log.info("Korrespondent Match: '%s' → '%s' (ID %s, default_dt=%s)",
                     raw_name, matched["name"], paperless_id, default_dt)
            return paperless_id, False, default_dt
        # In correspondents.json vorhanden aber noch nicht in Paperless angelegt
        log.info("Korrespondent in Map, noch keine Paperless-ID → anlegen: '%s'", matched["name"])
        try:
            new_id = _get_by_name("correspondents", matched["name"]) or                      _create_obj("correspondents", matched["name"])
            if new_id:
                matched.setdefault("_paperless", {})["id"] = new_id
                _save_corr_ids_only(corr_map)
                log.info("Korrespondent in Paperless angelegt + ID gespeichert: %s", new_id)
                return new_id, False, default_dt
        except Exception as e:
            log.warning("Korrespondent anlegen fehlgeschlagen: %s", e)
        return None, False, default_dt

    # ── Schritt 2: Fuzzy Match ────────────────────────────────────────────────
    fuzzy_entry, fuzzy_score = _find_fuzzy_match(corr_map, raw_name)
    log.info("Kein Exact Match für '%s' — Fuzzy-Score: %d%%", raw_name, fuzzy_score)

    # 3-Stufen Fuzzy — verhindert zu aggressive Merge-Vorschläge
    if fuzzy_score >= HIGH_THRESHOLD and fuzzy_entry:
        aktion = "ergaenzung"     # sehr ähnlich → wahrscheinlich Variante
    elif fuzzy_score >= MERGE_THRESHOLD and fuzzy_entry:
        aktion = "merge_into"     # ähnlich → möglicher Merge-Kandidat
    else:
        aktion = "neu"            # zu verschieden oder kein Treffer → neu anlegen
        fuzzy_entry = None

    pending_entry = _build_pending_entry(
        aktion, raw_name, document_id,
        fuzzy_match=fuzzy_entry, fuzzy_score=fuzzy_score,
        identifikatoren=_extract_identifikatoren_vorschlag(ocr_text, qr_meta, vision_meta),
    )
    idents = pending_entry.get("vorgeschlagener_eintrag", {}).get("identifikatoren", {})
    if any(idents.get(k) for k in ("uid", "iban", "email", "telefon")):
        log.info(
            "Identifikatoren-Vorschlag für '%s': uid=%s iban=%s email=%s tel=%s",
            raw_name, idents.get("uid"), idents.get("iban"),
            idents.get("email"), idents.get("telefon"),
        )
    _append_pending_corr(pending_entry)

    # Kein direkter PATCH hier — pending_review Tag wird im finalen Haupt-PATCH gesetzt
    # (verhindert dass der spätere Tags-PATCH pending_review wieder überschreibt)
    return None, True, None  # (kein Korrespondent, pending_review nötig, kein default_dt)

# Cache für bekannte Dokumenttypen
# Struktur: name.lower() → {"id": int, "synonyme": [str]}
_KNOWN_DOCTYPE_CACHE: dict = {}
_KNOWN_DOCTYPE_LOADED: bool = False
# Synonym-Map: synonym.lower() → kanonischer_name.lower()
_SYNONYM_MAP: dict = {}


def _load_known_doctypes() -> None:
    """Dokumenttypen aus Paperless + Synonyme aus document_types.json laden."""
    global _KNOWN_DOCTYPE_LOADED
    if _KNOWN_DOCTYPE_LOADED:
        return
    try:
        # 1. Paperless: IDs laden
        result = _http.get(
            f"{PAPERLESS_URL}/api/document_types/?page_size=500",
            headers=_headers(), timeout=15
        ).json()
        pl_map = {dt["name"].lower(): dt["id"] for dt in result.get("results", [])}

        # 2. document_types.json: Synonyme + Unique-Validierung
        if DOCUMENT_TYPES_JSON.exists():
            dt_data = json.loads(DOCUMENT_TYPES_JSON.read_text(encoding="utf-8"))
            seen = {}  # string.lower() → typ_name (Unique-Check)
            for typ in dt_data.get("typen", []):
                typ_name = typ["name"]
                typ_lower = typ_name.lower()
                # Name registrieren
                if typ_lower in seen:
                    log.warning("Dokumenttyp DUPLICATE NAME '%s' — ignoriert", typ_name)
                    continue
                seen[typ_lower] = typ_name
                dt_id = pl_map.get(typ_lower)
                _KNOWN_DOCTYPE_CACHE[typ_lower] = {"id": dt_id, "name": typ_name}
                # Synonyme registrieren
                for syn in typ.get("synonyme", []):
                    syn_lower = syn.lower()
                    if syn_lower in seen:
                        log.warning("Synonym DUPLICATE '%s' (bei '%s') — ignoriert", syn, typ_name)
                        continue
                    seen[syn_lower] = typ_name
                    _SYNONYM_MAP[syn_lower] = typ_lower
        else:
            # Fallback: nur Paperless-IDs ohne Synonyme
            for name_lower, dt_id in pl_map.items():
                _KNOWN_DOCTYPE_CACHE[name_lower] = {"id": dt_id, "name": name_lower}

        _KNOWN_DOCTYPE_LOADED = True
        log.info("Dokumenttyp-Cache: %d Typen, %d Synonyme geladen",
                 len(_KNOWN_DOCTYPE_CACHE), len(_SYNONYM_MAP))
    except Exception as e:
        log.warning("Dokumenttyp-Cache laden fehlgeschlagen: %s", e)


# Ausschluss-Map: typ_name.lower() → [keyword, ...] (case-insensitive Match)
# Kein Unique-Constraint — gleiche Keywords dürfen bei mehreren Typen stehen
_AUSSCHLUSS_MAP: dict[str, list[str]] = {}
_AUSSCHLUSS_LOADED: bool = False


def _load_ausschluss_map() -> None:
    """Ausschluss-Keywords aus document_types.json laden."""
    global _AUSSCHLUSS_LOADED
    if _AUSSCHLUSS_LOADED:
        return
    try:
        if DOCUMENT_TYPES_JSON.exists():
            dt_data = json.loads(DOCUMENT_TYPES_JSON.read_text(encoding="utf-8"))
            for t in dt_data.get("typen", []):
                kws = [k.lower() for k in t.get("ausschliessen", []) if k]
                if kws:
                    _AUSSCHLUSS_MAP[t["name"].lower()] = kws
        _AUSSCHLUSS_LOADED = True
        if _AUSSCHLUSS_MAP:
            log.info("Ausschluss-Map geladen: %d Typen mit Ausschluss-Keywords", len(_AUSSCHLUSS_MAP))
    except Exception as e:
        log.warning("Ausschluss-Map laden fehlgeschlagen: %s", e)


def _doctype_is_excluded(typ_name: str, ocr_text: str, vision_meta: dict) -> bool:
    """Prüft ob ein Dokumenttyp für dieses Dokument ausgeschlossen ist.
    Matched Case-Insensitiv gegen OCR-Text + Vision-Absender + Vision-Dokumenttyp.
    """
    _load_ausschluss_map()
    kws = _AUSSCHLUSS_MAP.get(typ_name.lower(), [])
    if not kws:
        return False
    # Suchraum: OCR (erste 2000 Zeichen) + Vision-Absender + Vision-Dokumenttyp
    search_text = " ".join(filter(None, [
        ocr_text[:2000] if ocr_text else "",
        str(vision_meta.get("absender") or ""),
        str(vision_meta.get("dokumenttyp_visuell") or ""),
    ])).lower()
    for kw in kws:
        if kw in search_text:
            log.info("Dokumenttyp '%s' ausgeschlossen: Keyword '%s' gefunden", typ_name, kw)
            return True
    return False


def _resolve_doctype_via_ollama(name: str) -> Optional[str]:
    """Fallback: Ollama fragt welcher Dokumenttyp am nächsten ist.
    Gibt kanonischen Namen zurück oder None.
    """
    if not _KNOWN_DOCTYPE_CACHE:
        return None
    typ_liste = ", ".join(
        f'"{v["name"]}"' for v in _KNOWN_DOCTYPE_CACHE.values() if v.get("id")
    )
    if not typ_liste:
        return None
    prompt = (
        f'Welcher dieser Dokumenttypen passt am besten zu "{name}"? '
        f'Antworte NUR mit dem exakten Namen aus dieser Liste, ohne Erklärung: {typ_liste}'
    )
    try:
        r = _http.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": MODEL_LLM, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0, "num_predict": 30}},
            timeout=15
        )
        antwort = r.json().get("response", "").strip().strip('"').strip("'")
        # Antwort gegen Cache prüfen
        if antwort.lower() in _KNOWN_DOCTYPE_CACHE:
            log.info("Dokumenttyp Ollama-Fallback: '%s' → '%s'", name, antwort)
            return antwort.lower()
    except Exception as e:
        log.warning("Ollama Dokumenttyp-Fallback fehlgeschlagen: %s", e)
    return None


def resolve_document_type(name: str, ocr_text: str = "", vision_meta: dict = None) -> Optional[int]:
    """Dokumenttyp-ID auflösen — 3-Stufen-Suche:
    1. Exakter Name-Match
    2. Synonym-Match (unique, in document_types.json definiert)
    3. Ollama-Fallback (semantische Ähnlichkeit)
    Neue Dokumenttypen werden NICHT angelegt.
    Ausschluss-Check: wenn Ausschluss-Keywords im Dokument → None zurück.
    """
    if not name or name.lower() in ("null", "none", "sonstiges", ""):
        return None
    _load_known_doctypes()
    name_lower = name.lower()

    # Stufe 1: Exakter Name-Match
    if name_lower in _KNOWN_DOCTYPE_CACHE:
        # Ausschluss-Check
        if _doctype_is_excluded(name_lower, ocr_text, vision_meta or {}):
            log.info("Dokumenttyp '%s' via Ausschluss-Check verworfen", name)
            return None
        return _KNOWN_DOCTYPE_CACHE[name_lower].get("id")

    # Stufe 2: Synonym-Match
    canonical = _SYNONYM_MAP.get(name_lower)
    if canonical and canonical in _KNOWN_DOCTYPE_CACHE:
        if _doctype_is_excluded(canonical, ocr_text, vision_meta or {}):
            log.info("Dokumenttyp '%s' (via Synonym '%s') via Ausschluss-Check verworfen", canonical, name)
            return None
        dt_id = _KNOWN_DOCTYPE_CACHE[canonical].get("id")
        log.info("Dokumenttyp Synonym: '%s' → '%s' (ID %s)", name, canonical, dt_id)
        return dt_id

    # Stufe 3: Ollama-Fallback
    ollama_match = _resolve_doctype_via_ollama(name)
    if ollama_match and ollama_match in _KNOWN_DOCTYPE_CACHE:
        if _doctype_is_excluded(ollama_match, ocr_text, vision_meta or {}):
            log.info("Dokumenttyp '%s' (Ollama-Fallback) via Ausschluss-Check verworfen", ollama_match)
            return None
        return _KNOWN_DOCTYPE_CACHE[ollama_match].get("id")

    log.info("Dokumenttyp '%s' nicht auflösbar — ignoriert", name)
    return None


def resolve_storage_path_with_template(name: str, template: str) -> Optional[int]:
    """Storage Path nach Name — Template explizit (z. B. legacy/{title})."""
    if not name or not template:
        return None
    try:
        sp_id = _get_by_name("storage_paths", name)
        if sp_id:
            return sp_id
        return _create_obj("storage_paths", name, {"path": template})
    except Exception as e:
        log.warning("Storage Path '%s': %s", name, e)
        return None


def resolve_storage_path(pfad: str) -> Optional[int]:
    """Storage Path suchen oder anlegen (Pipeline: pfad/{{ created_year }}/{{ title }})."""
    if not pfad:
        return None
    template = f"{pfad}/{{{{ created_year }}}}/{{{{ title }}}}"
    return resolve_storage_path_with_template(pfad, template)


def ensure_all_storage_paths(manifest: list[dict]):
    """Alle Manifest-Pfade in Paperless sicherstellen."""
    for entry in manifest:
        pfad = entry.get("pfad")
        if pfad:
            resolve_storage_path(pfad)


# ─── Hauptlogik ───────────────────────────────────────────────────────────────

def _sanitize_titel(titel: str) -> str:
    """Bereinigt den Dokumenttitel für Paperless:
    - Spaces → Underscore
    - Mehrfache Underscores → einfach
    - Führende/abschliessende Underscores entfernen
    - Unerlaubte Dateisystem-Zeichen entfernen (/ \\ : * ? " < > |)
    - Umlaut-Umschreibung absichtlich NICHT — ä/ö/ü/ß bleiben erhalten
    """
    import re
    t = titel.strip()
    # Dateisystem-Sonderzeichen entfernen (nicht ersetzen — verfälscht den Titel)
    t = re.sub(r'[/\\:*?"<>|]', '', t)
    # Whitespace (Spaces, Tabs, Newlines von LLM) → Underscore
    t = re.sub(r'\s+', '_', t)
    # Mehrfache Underscores → einfach
    t = re.sub(r'_+', '_', t)
    # Führende/abschliessende Underscores
    t = t.strip('_')
    return t


def _make_unique_titel(titel: str, ordner: str) -> str:
    """Letzter Sicherheitsnetz: Laufnummer anhängen falls Titel+Ordner trotzdem noch kollidiert.

    Datum und Kürzel wurden bereits in main() angehängt — dieser Check fängt nur
    den unwahrscheinlichen Fall ab dass zwei identische Dokumente im gleichen Monat
    vom gleichen Korrespondenten ankommen.
    """
    def _exists(t: str) -> bool:
        try:
            result = paperless_get("/documents/", params={"title__icontains": t, "page_size": 25})
            if not isinstance(result, dict) or not result.get("results"):
                return False
            for doc in result["results"]:
                if (doc.get("title") or "").lower() != t.lower():
                    continue
                sp = doc.get("storage_path")
                if sp is None:
                    continue
                sp_name = _storage_path_name_by_id(sp)
                if sp_name and ordner and sp_name.lower().startswith(ordner.lower()):
                    return True
            return False
        except Exception as e:
            log.warning("Uniqueness-Check fehlgeschlagen: %s", e)
            return False

    if not _exists(titel):
        return titel

    for n in range(2, 20):
        tn = f"{titel}_{n}"
        if not _exists(tn):
            log.warning("Titel-Uniqueness: Laufnummer nötig → '%s'", tn)
            return tn

    log.error("Titel-Uniqueness: konnte keinen eindeutigen Titel finden für '%s'", titel)
    return titel


def _storage_path_name_by_id(sp_id: int) -> str:
    """Gibt Storage-Path-Name für eine ID zurück (gecacht)."""
    if not hasattr(_storage_path_name_by_id, "_cache"):
        _storage_path_name_by_id._cache = {}
    if sp_id in _storage_path_name_by_id._cache:
        return _storage_path_name_by_id._cache[sp_id]
    try:
        result = paperless_get(f"/storage_paths/{sp_id}/")
        name = result.get("name", "")
        _storage_path_name_by_id._cache[sp_id] = name
        return name
    except Exception:
        return ""


def main():
    log.info("=" * 70)
    log.info("post_consume_v%s | ID=%s | Datei=%s", POST_CONSUME_VERSION, DOCUMENT_ID, DOCUMENT_FILE_NAME)
    log.info("Storage-Modus: %s | Vision-Modell: %s", STORAGE_MODE, MODEL_VISION)

    if not DOCUMENT_ID:
        log.error("DOCUMENT_ID nicht gesetzt — Abbruch")
        sys.exit(1)
    if not PAPERLESS_TOKEN:
        log.error("PAPERLESS_TOKEN nicht gesetzt — Abbruch")
        sys.exit(1)

    document_id = int(DOCUMENT_ID)

    # Dok-ID sofort setzen — unabhängig von LLM/Vision; überlebt Pipeline-Abbruch
    ensure_dok_id(document_id)

    start_time = time.monotonic()

    # ── Manifest + Korrekturen laden ──────────────────────────────────────────
    manifest    = load_manifest()
    corrections = load_corrections()
    log.info("Manifest: %d Einträge | Korrekturen: %d", len(manifest), len(corrections))

    # ── Manifest-Embeddings laden/cachen ─────────────────────────────────────
    manifest_embeddings = load_or_build_manifest_embeddings(manifest)

    # ── Storage Paths sicherstellen (gecacht via Lockfile) ───────────────────
    # Nur laden wenn noch kein Cache existiert — nicht für jedes Dokument neu
    # SP-Cache an Manifest-Hash koppeln — selbstheilend bei Manifest-Änderungen
    _sp_cache = Path("/tmp/paperless_sp_cache.flag")
    _current_mhash = _manifest_hash(manifest)
    _cached_mhash  = _sp_cache.read_text().strip() if _sp_cache.exists() else ""
    if STORAGE_MODE == "api" and _current_mhash != _cached_mhash:
        ensure_all_storage_paths(manifest)
        try:
            _sp_cache.write_text(_current_mhash)
        except Exception:
            pass

    # ── OCR-Text aus Paperless abrufen ────────────────────────────────────────
    try:
        doc_info = _http.get(
            f"{PAPERLESS_URL}/api/documents/{document_id}/",
            headers=_headers(), timeout=30
        ).json()
        ocr_text_full = doc_info.get("content", "").strip()
        # Auf 3000 Zeichen kürzen — reicht für Klassifizierung, vermeidet Timeout bei langen Docs
        ocr_text = ocr_text_full[:3000]
    except Exception as e:
        log.warning("Dokument-Info Abruf fehlgeschlagen: %s", e)
        ocr_text = ""
        ocr_text_full = ""

    if not ocr_text:
        log.warning("Kein OCR-Text — Vision-LLM arbeitet nur mit Bild")
    elif len(ocr_text_full) > 3000:
        log.info("OCR-Text gekürzt: %d → 3000 Zeichen", len(ocr_text_full))

    # ── PDF finden ────────────────────────────────────────────────────────────
    pdf_path = resolve_document_pdf(DOCUMENT_ID) if DOCUMENT_ID else None
    if pdf_path:
        log.info("PDF: %s", pdf_path)
    else:
        log.warning("PDF nicht gefunden: %s (Dok #%s)", DOCUMENT_FILE_NAME, DOCUMENT_ID)

    # ── QR-Meta aus pre_consume_qr.py lesen ──────────────────────────────────
    qr_meta = read_qr_meta(DOCUMENT_SOURCE_PATH)
    if qr_meta:
        log.info("QR-Bill: Betrag=%s %s, Referenz=%s",
                 qr_meta.get("betrag"), qr_meta.get("waehrung"), qr_meta.get("referenz"))

    # ── Schritt 2: Vision-LLM (immer) ────────────────────────────────────────
    # Vision wird IMMER ausgeführt — sie liefert strukturierte Felder
    # (Absender, Datum, Betrag, Handschrift) die OCR allein nicht zuverlässig extrahiert.
    # OCR-Keywords werden nur noch als Zusatzinfo ans LLM übergeben, nicht als Bypass.
    image_b64 = None
    if pdf_path:
        image_b64 = pdf_to_base64_image(pdf_path)
    log.info("Schritt 2: Vision-LLM (%s)", MODEL_VISION)
    vision_meta = _disambiguate_vision_money_fields(vision_analyze(image_b64, ocr_text))

    _htr_pre_resolution = None
    _needs_pending_htr_decision = False
    corr_map_htr = _load_corr_map()
    _early_corr, _early_ident = _match_correspondent_by_identifikatoren(
        corr_map_htr, ocr_text, qr_meta=qr_meta, vision_meta=vision_meta,
    )
    _early_corr_htr = _early_corr if _early_ident in ("UID", "IBAN", "E-Mail") else None
    _htr_pre_resolution = decide_htr_action(
        vision_meta,
        ocr_text,
        correspondent=_early_corr_htr,
        correspondent_match=_early_ident if _early_corr_htr else None,
    )
    _htr_audit = _htr_pre_resolution.to_audit_dict()
    if _htr_pre_resolution.config:
        _htr_audit["crop_mode_effective"] = _htr_pre_resolution.crop_mode_effective
    write_audit_entry(document_id, "htr_pre_resolution", _htr_audit)

    if pdf_path and _htr_pre_resolution.action == HTR_ACTION_RUN:
        log.info(
            "Handschrift — HTR %s (Profil '%s', confidence=%s, crop=%s, %d dpi)",
            _htr_pre_resolution.action,
            _htr_pre_resolution.profile_name,
            _htr_pre_resolution.profile_confidence,
            _htr_pre_resolution.crop_mode_effective,
            (_htr_pre_resolution.config.dpi if _htr_pre_resolution.config else SCHULBERICHT_DPI),
        )
        htr_deps = HtrPipelineDeps(
            pdf_to_b64=_schulbericht_pdf_to_b64,
            htr_page=vision_htr_page,
            schulbericht_page_e2e=vision_schulbericht_page,
            extract_schulbericht=extract_schulbericht_from_transcript,
        )
        htr_meta = run_htr_pipeline(_htr_pre_resolution, pdf_path, ocr_text, htr_deps)
        if htr_meta:
            vision_meta = {**vision_meta, **htr_meta}
        if _htr_pre_resolution.variants:
            write_audit_entry(document_id, "htr", {
                **_htr_audit,
                "variants": _htr_pre_resolution.variants,
            })
    elif _htr_pre_resolution.action == HTR_ACTION_DEFER:
        log.info(
            "HTR deferred — Profil unsicher (source=%s), pending_htr_decision",
            _htr_pre_resolution.htr_profile_source,
        )
        _needs_pending_htr_decision = True
        write_pending_htr_decision(document_id, _htr_audit)
    log.info("Vision: %s", json.dumps(vision_meta, ensure_ascii=False))
    write_audit_entry(document_id, "vision", vision_meta)

    # Vision leer → confidence später auf mittel setzen (Doc-Review erzwingen)
    _vision_empty = not vision_meta or vision_meta == {}

    # ── Schritt 3: bge-m3 RAG ─────────────────────────────────────────────────
    log.info("Schritt 3: bge-m3 RAG (Top-%d)", RAG_TOP_K)
    rag_text = ocr_text or f"{DOCUMENT_FILE_NAME} {vision_meta.get('dokumenttyp_visuell', '')}"
    similar_entries = find_similar_manifest_entries(rag_text, manifest, manifest_embeddings, top_k=RAG_TOP_K)

    # ── Schritt 3.5: Deterministisches Pre-Routing ───────────────────────────
    # Reihenfolge: 1) Kennzeichen, 2) Beziehungen (Arbeitgeber, Bank, etc.)
    kz_vision = _norm_kz_key(str(vision_meta.get("kennzeichen") or ""))
    kz_ocr    = ocr_text.upper().replace(" ", "")
    pre_decision = None

    def _extract_absender_from_ocr(text: str) -> Optional[str]:
        """Versucht Absender aus den ersten 3 Zeilen des OCR zu extrahieren."""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return lines[0] if lines else None

    def _pre_route(ordner: str, kz_tag: str, source: str, fz_default_tag: str = "") -> dict:
        """Baut pre_decision aus Manifest-Tags — kein Hardcoding.
        Korrespondent kommt aus correspondents.json (typische_ordner Match),
        NICHT aus Vision/OCR — Vision kann Handschrift oder Störtext als Absender lesen.
        """
        # Korrespondent aus correspondents.json — wer hat diesen Ordner als typischen Ordner?
        _korrespondent = None
        try:
            corr_data = json.loads(CORRESPONDENTS_JSON.read_text(encoding="utf-8"))
            for e in corr_data.get("eintraege", []):
                if ordner in e.get("typische_ordner", []):
                    _korrespondent = e["name"]
                    break
        except Exception:
            pass
        # Fallback: Vision-Absender nur wenn kein Eintrag in correspondents.json
        if not _korrespondent:
            _korrespondent = vision_meta.get("absender") or _extract_absender_from_ocr(ocr_text)

        ordner_entry = next((e for e in manifest if e.get("pfad") == ordner), {})
        manifest_tags = ordner_entry.get("erlaubte_tags", [])
        _max = ordner_entry.get("max_tags", 4)
        auto_tags: list[str] = []
        if fz_default_tag and fz_default_tag not in auto_tags:
            auto_tags.append(fz_default_tag)
        if kz_tag in manifest_tags and kz_tag not in auto_tags:
            auto_tags.append(kz_tag)
        for t in manifest_tags:
            if t not in auto_tags and len(auto_tags) < _max:
                auto_tags.append(t)
        # Dokumenttyp nur aus Manifest primär — muss in Paperless existieren
        doctyp = (ordner_entry.get("dokumenttyp") or {}).get("primär") or ""
        if doctyp:
            _load_known_doctypes()
            _dt_key = doctyp.lower()
            if _dt_key not in _KNOWN_DOCTYPE_CACHE or not _KNOWN_DOCTYPE_CACHE[_dt_key].get("id"):
                log.info("Pre-Route: Manifest-Dokumenttyp '%s' nicht in Paperless — ignoriert", doctyp)
                doctyp = ""
        _vis_typ = (vision_meta.get("dokumenttyp_visuell") or "").strip()
        _titel_typ = doctyp or _vis_typ or "Dokument"
        _titel = f"{_titel_typ} {_korrespondent}".strip() if _korrespondent else _titel_typ
        return {
            "ordner": ordner,
            "tags": auto_tags,
            "korrespondent": _korrespondent,
            "titel": _titel,
            "datum": vision_meta.get("datum"),
            "betrag": vision_meta.get("betrag"),
            "dokumenttyp_semantisch": doctyp,
            "confidence": "hoch",
            "begruendung": f"Deterministisch: Kennzeichen {kz_tag} ({source}) → {ordner}"
        }

    # Kennzeichen: CF/Person immer bei Match; Ordner-Routing nur wenn routing_ordner
    _family_kz_match = None
    kz_map = _build_kennzeichen_map()
    for kz_norm, fz in kz_map.items():
        kz_display = fz["kennzeichen_display"]
        ordner     = fz["ordner"]
        kz_in_vision = kz_norm in kz_vision
        kz_in_ocr    = kz_norm in kz_ocr
        if not (kz_in_vision or kz_in_ocr):
            continue
        source = "vision_meta" if kz_in_vision else "OCR-Text"
        _family_kz_match = (fz, source)
        if fz.get("routing_ordner") and ordner:
            log.info("Pre-Routing: Kennzeichen %s (%s) → %s (kein LLM)", kz_display, source, ordner)
            pre_decision = _pre_route(ordner, kz_display, source, fz_default_tag=fz.get("default_tag", ""))
            _kz_person = _resolve_person_anzeigename(fz.get("person_id", ""))
            if _kz_person:
                pre_decision["_bez_person"] = _kz_person
        else:
            log.info(
                "Kennzeichen %s (%s) erkannt — CF/Person only (routing_ordner aus)",
                kz_display, source,
            )
        break  # erstes Match gewinnt

    # ── Stufe 1: Beziehungs-Routing aus correspondents.json ──────────────────
    corr_map       = _load_corr_map()
    vision_absender  = (vision_meta.get("absender") or "").strip()
    vision_empfaenger = (vision_meta.get("empfaenger") or "").strip()
    _corr_entry    = None   # gefundener Korrespondenten-Eintrag
    _beziehung     = None   # gefundene Beziehung
    _stufe1_grund  = ""     # warum kein Stufe-1-Match (für LLM-Prompt)
    _ident_grund   = ""     # UID / IBAN / Telefon

    if not pre_decision:
        _corr_entry, _ident_grund = _match_correspondent_by_identifikatoren(
            corr_map, ocr_text, qr_meta=qr_meta, vision_meta=vision_meta,
        )
        if _corr_entry:
            log.info(
                "Stufe 1: Korrespondent '%s' via Identifikator (%s)",
                _corr_entry["name"], _ident_grund,
            )
        if not _corr_entry:
            _corr_entry = _match_korrespondent_eintrag(corr_map, vision_absender)
        if _corr_entry:
            log.info("Stufe 1: Korrespondent '%s' gefunden", _corr_entry["name"])
            if _htr_pre_resolution:
                _dt_used = _htr_pre_resolution.document_type_used
                if not _dt_used:
                    raw_vis = vision_meta.get("dokumenttyp_visuell")
                    _dt_used, _ = normalize_document_type_key(str(raw_vis) if raw_vis else None)
                _missed = audit_missed_correspondent_override(
                    _htr_pre_resolution, _corr_entry, document_type_used=_dt_used,
                )
                if _missed:
                    write_audit_entry(document_id, "htr_override_missed", _missed)
            _beziehung = _match_beziehung_v2(_corr_entry, vision_empfaenger, ocr_text,
                                                      dokumenttyp_visuell=vision_meta.get("dokumenttyp_visuell", ""),
                                                      vision_meta=vision_meta)
            if _beziehung:
                bez_ordner   = _beziehung.get("ordner", "")
                bez_doctypen = _beziehung.get("erlaubte_doctypen", [])
                bez_person   = _beziehung.get("person", "")
                bez_bez      = _beziehung.get("bezeichnung", "")
                log.info("Stufe 1: Beziehungs-Match '%s' → person=%s ordner=%s",
                         bez_bez, bez_person, bez_ordner)

                # Ordner: immer deterministisch (1 Ordner pro Beziehung)
                # Doctyp: nur wenn genau 1 Option → deterministisch
                if len(bez_doctypen) == 1:
                    bez_doctyp = bez_doctypen[0]
                    log.info("Stufe 1: Doctyp deterministisch → %s", bez_doctyp)
                else:
                    bez_doctyp = None  # LLM wählt aus bez_doctypen
                    log.info("Stufe 1: %d Doctypen → LLM wählt aus %s", len(bez_doctypen), bez_doctypen)

                # fix_tags: Beziehungsebene + Korrespondenten-Ebene kombinieren
                bez_fix_tags  = _beziehung.get("fix_tags", [])
                korr_fix_tags = _corr_entry.get("fix_tags", [])
                fix_tags = list(dict.fromkeys(bez_fix_tags + korr_fix_tags))

                pre_decision = {
                    "ordner":                bez_ordner,
                    "tags":                  fix_tags,
                    "korrespondent":         _corr_entry["name"],
                    "titel":                 vision_meta.get("dokumenttyp_visuell") or bez_bez,
                    "datum":                 vision_meta.get("datum"),
                    "betrag":                vision_meta.get("betrag"),
                    "dokumenttyp_semantisch": bez_doctyp or "",
                    "confidence":            "hoch",
                    "begruendung":           f"Stufe 1: Beziehung '{bez_bez}' (person={bez_person}) → {bez_ordner}",
                    "_bez_doctypen_offen":   bez_doctypen if not bez_doctyp else [],
                    "_bez_person":           _resolve_person_anzeigename(bez_person),
                }
            else:
                _stufe1_grund = (
                    f"Korrespondent '{_corr_entry['name']}' bekannt, "
                    f"aber keine passende Beziehung für Empfänger='{vision_empfaenger}'"
                )
                log.info("Stufe 1: kein Beziehungs-Match — %s", _stufe1_grund)
        else:
            _stufe1_grund = f"Korrespondent '{vision_absender}' nicht in correspondents.json"

    # ── Stufe 2: fix_tags + Beziehungs-Routing aus family.json (Legacy-Fallback) ──
    if not pre_decision:
        # Stufe 2a: fix_tags aus Korrespondent (falls gefunden aber kein Beziehungs-Match)
        # Diese werden später dem LLM-Ergebnis hinzugefügt
        _corr_fix_tags = _corr_entry.get("fix_tags", []) if _corr_entry else []

        # Stufe 2b: family.json Beziehungen (Legacy — Arbeitgeber etc.)
        bez = _match_beziehung(vision_absender)
        if bez:
            bez_ordner = bez.get("ordner", "")
            bez_korr   = bez.get("korrespondent", vision_absender)
            bez_typ    = bez.get("typ", "beziehung")
            log.info("Stufe 2b: family.json Beziehung '%s' (%s) → %s", bez_korr, bez_typ, bez_ordner)
            ordner_entry = next((e for e in manifest if e.get("pfad") == bez_ordner), {})
            manifest_tags = ordner_entry.get("erlaubte_tags", [])
            _max = ordner_entry.get("max_tags", 4)
            auto_tags = list(dict.fromkeys(_corr_fix_tags + manifest_tags[:_max]))
            pre_decision = {
                "ordner":                bez_ordner,
                "tags":                  auto_tags,
                "korrespondent":         bez_korr,
                "titel":                 vision_meta.get("dokumenttyp_visuell") or bez_typ.capitalize(),
                "datum":                 vision_meta.get("datum"),
                "betrag":                vision_meta.get("betrag"),
                "dokumenttyp_semantisch": "",
                "confidence":            "hoch",
                "begruendung":           f"Stufe 2b: family.json Beziehung '{bez_typ}' '{bez_korr}' → {bez_ordner}",
                "_bez_doctypen_offen":   [],
            }

    # ── Schritt 4: LLM Entscheidung ───────────────────────────────────────────
    log.info("Schritt 4: %s Entscheidung", MODEL_LLM)

    # Constraints einmal berechnen — für Prompt UND Validation-Layer
    constraints = build_constraints(similar_entries)
    allowed_tags          = constraints["allowed_tags"]
    verboten_tags         = constraints["verboten_tags"]
    allowed_dokumenttypen = constraints["allowed_dokumenttypen"]
    max_tags              = constraints["max_tags"]

    # Korrespondenten-Constraints (Stufe 2: verbotene_* + fix_tags)
    _verbotene_doctypen = []
    _verbotene_ordner   = []
    _verbotene_tags     = []
    _corr_fix_tags_global = []
    if _corr_entry:
        _verbotene_doctypen   = _corr_entry.get("verbotene_doctypen", [])
        _verbotene_ordner     = _corr_entry.get("verbotene_ordner", [])
        _verbotene_tags       = _corr_entry.get("verbotene_tags", [])
        _corr_fix_tags_global = _corr_entry.get("fix_tags", [])

    if pre_decision:
        # Stufe 1: fix_tags aus Beziehung + Korrespondent bereits in pre_decision["tags"]
        # (gesetzt in _match_beziehung_v2 Block oben) — kein zweites Hinzufügen nötig
        # Nur noch: offene Doctypen via LLM wählen lassen
        bez_doctypen_offen = pre_decision.pop("_bez_doctypen_offen", [])
        if bez_doctypen_offen and not pre_decision.get("dokumenttyp_semantisch"):
            log.info("Stufe 1: LLM wählt Doctyp aus %s", bez_doctypen_offen)
            dt_constraints = {**constraints, "allowed_dokumenttypen": set(bez_doctypen_offen)}
            # Hinweis: verbotene_tags/_ordner/_doctypen hier nicht relevant —
            # LLM wählt nur noch den Doctyp aus einer expliziten Positivliste (bez_doctypen_offen).
            # verbotene_* greifen nur im Stufe-2/3 Pfad (voller LLM-Call).
            dt_prompt = build_llm_prompt(
                ocr_text, vision_meta, similar_entries, manifest, corrections,
                DOCUMENT_FILE_NAME, dt_constraints,
                corr_map=corr_map,
                stufe1_kontext={
                    "beziehung": _beziehung,
                    "korrespondent": _corr_entry,
                    "nur_doctyp": True,
                }
            )
            dt_decision = llm_decide(dt_prompt)
            if dt_decision:
                dt_decision = normalize_decision_keys(dt_decision)
                pre_decision["dokumenttyp_semantisch"] = dt_decision.get("dokumenttyp_semantisch", "")

        decision = pre_decision

    else:
        # Stufe 2/3: LLM mit vollem Kontext + verbotene_* + fix_tags
        # fix_tags werden nach LLM hinzugefügt
        stufe_kontext = {
            "beziehungen_korrespondent": _corr_entry.get("beziehungen", []) if _corr_entry else [],
            "stufe1_grund":    _stufe1_grund,
            "verbotene_doctypen": _verbotene_doctypen,
            "verbotene_ordner":   _verbotene_ordner,
            "verbotene_tags":     _verbotene_tags,
            "bevorzugte_ordner":  _corr_entry.get("typische_ordner", []) if _corr_entry else [],
        }
        prompt = build_llm_prompt(
            ocr_text, vision_meta, similar_entries, manifest, corrections,
            DOCUMENT_FILE_NAME, constraints,
            corr_map=corr_map,
            stufe1_kontext=stufe_kontext,
        )
        decision = llm_decide(prompt)
        if not decision:
            log.error("Keine verwertbare Entscheidung — Exit 0")
            sys.exit(0)
        decision = normalize_decision_keys(decision)

        # fix_tags aus Korrespondent immer hinzufügen (Stufe 2)
        if _corr_fix_tags_global:
            existing = decision.get("tags", [])
            decision["tags"] = list(dict.fromkeys(existing + _corr_fix_tags_global))
            log.info("Stufe 2: fix_tags hinzugefügt: %s", _corr_fix_tags_global)

    # ── Sanitization direkt nach LLM-Output ──────────────────────────────────
    try:
        decision, had_violations = sanitize_decision(decision, manifest, similar_entries)
    except Exception as e:
        log.error("sanitize_decision fehlgeschlagen — mit Roh-Decision weiter: %s", e, exc_info=True)
        had_violations = True
        for _k in ("korrespondent", "titel", "dokumenttyp_semantisch", "ordner", "begruendung"):
            if decision.get(_k) is None:
                decision[_k] = ""
        if decision.get("confidence") not in ("hoch", "mittel", "tief"):
            decision["confidence"] = "tief"

    # Beziehungsvorschlag aus LLM-Output extrahieren (Stufe 3 → pending_beziehungen)
    bez_vorschlag = decision.pop("beziehungs_vorschlag", None)
    if bez_vorschlag and isinstance(bez_vorschlag, dict) and bez_vorschlag.get("person") and bez_vorschlag.get("ordner"):
        korr_name = _corr_entry["name"] if _corr_entry else (vision_meta.get("absender") or "")
        write_pending_beziehung(bez_vorschlag, document_id, korr_name)
        log.info("Beziehungs-Vorschlag gespeichert: person=%s ordner=%s",
                 bez_vorschlag.get("person"), bez_vorschlag.get("ordner"))

    # Vision leer → confidence auf mittel erzwingen (Doc-Review)
    if _vision_empty and decision.get("confidence") == "hoch":
        decision["confidence"] = "mittel"
        log.warning("Vision leer — Confidence hoch→mittel (Doc-Review erzwungen)")

    # fix_tags aus Dokumenttyp hinzufügen (deterministisch, nach Sanitizer)
    doctyp_name = decision.get("dokumenttyp_semantisch", "")
    dt_fix_tags = _get_doctype_fix_tags(doctyp_name)
    if dt_fix_tags:
        existing = decision.get("tags", [])
        decision["tags"] = list(dict.fromkeys(existing + dt_fix_tags))
        log.info("DocType fix_tags '%s' → %s", doctyp_name, dt_fix_tags)

    # Constraints für nachgelagerte Nutzung (Tags-PATCH, Eskalation etc.)
    final_constraints     = build_constraints(similar_entries, target_ordner=decision["ordner"])
    allowed_tags          = final_constraints["allowed_tags"]
    verboten_tags         = final_constraints["verboten_tags"]
    allowed_dokumenttypen = final_constraints["allowed_dokumenttypen"]
    max_tags              = final_constraints["max_tags"]

    log.info("Entscheidung: %s", json.dumps(decision, ensure_ascii=False))
    log.info("Begründung: %s", decision.get("begruendung", ""))
    write_audit_entry(document_id, "sanitized", decision)

    # Identifikator-Match: Korrespondent setzen (UID/IBAN überschreibt LLM)
    if _corr_entry and _ident_grund:
        existing = (decision.get("korrespondent") or "").strip()
        if _ident_grund in ("UID", "IBAN", "E-Mail") or not existing:
            if existing and existing.lower() != _corr_entry["name"].lower():
                log.info(
                    "Korrespondent überschrieben: LLM '%s' → Identifikator %s '%s'",
                    existing, _ident_grund, _corr_entry["name"],
                )
            decision["korrespondent"] = _corr_entry["name"]
            begr = (decision.get("begruendung") or "").strip()
            id_note = f"Korrespondent via {_ident_grund}: {_corr_entry['name']}"
            decision["begruendung"] = f"{begr}\n{id_note}".strip() if begr else id_note
            if _ident_grund in ("UID", "IBAN", "E-Mail") and decision.get("confidence") in ("tief", "mittel"):
                decision["confidence"] = "hoch"
                log.info("Confidence → hoch (Identifikator %s)", _ident_grund)
            elif _ident_grund == "Telefon" and decision.get("confidence") == "tief":
                decision["confidence"] = "mittel"

    # ── Systemische Confidence-Anpassung ─────────────────────────────────────
    # Kleine Modelle setzen fast alles auf "hoch" — wir korrigieren systemisch
    _conf = decision.get("confidence", "tief")
    _downgrades = []
    if not decision.get("tags"):
        _downgrades.append("keine Tags")
    if not decision.get("korrespondent"):
        _downgrades.append("kein Korrespondent")
    if not vision_meta or vision_meta == {}:
        _downgrades.append("Vision fehlgeschlagen")
    if decision.get("ordner") == "Familie/Sonstiges":
        _downgrades.append("Fallback-Ordner")
    if len(_downgrades) >= 3 and _conf in ("hoch", "mittel"):
        decision["confidence"] = "tief"
        log.info("Confidence downgrade →tief: %s", ", ".join(_downgrades))
    elif len(_downgrades) >= 2 and _conf == "hoch":
        decision["confidence"] = "mittel"
        log.info("Confidence downgrade hoch→mittel: %s", ", ".join(_downgrades))

    # ── Schritt 5: Paperless API Patch ───────────────────────────────────────
    patch = {}

    # HTR-Transkript in durchsuchbaren Content (Paperless-Volltextsuche)
    _htr_search = extract_htr_searchable_text(vision_meta)
    if _htr_search:
        patch["content"] = build_htr_content_append(
            ocr_text_full or ocr_text,
            _htr_search,
            drop_ocr=is_schulbericht_htr_meta(vision_meta),
        )
        log.info("HTR in Document-Content: %d Zeichen", len(_htr_search))

    # Zentrale Permissions — verhindert "Private"-Anzeige für andere User
    patch.update(_default_permissions())

    # Custom Fields setzen (Betrag, QR-Referenz, Fällig am, Kennzeichen, etc.)
    # Korrespondenten-Eintrag: Stufe-1-Match behalten, sonst fuzzy/substring (nicht nur exakter Name)
    _corr_map: dict = {}
    try:
        _corr_map = _load_corr_map()
        if not _corr_entry:
            _raw_corr = (decision.get("korrespondent") or vision_absender or "").strip()
            _corr_entry = _resolve_corr_entry(_corr_map, _raw_corr)
    except Exception:
        pass

    # Titel — Sanitizer + immer YYYY-MM + Kürzel anhängen
    titel = (decision.get("titel") or "").strip()
    if titel:
        titel = _sanitize_titel(titel)
        # Kürzel aus Korrespondenten-Eintrag holen
        _kuerzel = ""
        if _corr_entry:
            _kuerzel = (_corr_entry.get("kuerzel") or "").strip().upper()
        # Datum-Suffix: YYYY-MM aus validiertem Datum
        _datum_raw = decision.get("datum") or ""
        _datum_suffix = ""
        if _datum_raw:
            _parts = str(_datum_raw).split("-")
            if len(_parts) >= 2:
                _datum_suffix = f"{_parts[0]}-{_parts[1]}"
            elif len(_parts) == 1 and len(_parts[0]) == 4:
                _datum_suffix = _parts[0]
        # Immer anhängen: _YYYY-MM und/oder _KÜRZEL (falls verfügbar)
        if _datum_suffix and _kuerzel:
            titel = f"{titel}_{_datum_suffix}_{_kuerzel}"
        elif _datum_suffix:
            titel = f"{titel}_{_datum_suffix}"
        elif _kuerzel:
            titel = f"{titel}_{_kuerzel}"
        # Kollisions-Fallback: Laufnummer falls Datum+Kürzel immer noch nicht reicht
        titel = _make_unique_titel(titel=titel, ordner=decision.get("ordner", ""))
        patch["title"] = titel[:128]

    # Korrespondent
    korr_id, pending_review_needed, corr_default_dt = resolve_correspondent_canonical(
        decision.get("korrespondent") or "",
        document_id=document_id,
        ocr_text=ocr_text_full,
        qr_meta=qr_meta,
        vision_meta=vision_meta,
    )
    if korr_id:
        patch["correspondent"] = korr_id
        # Paperless-ID ist autoritativ (LLM/Vision-String kann Filialname sein)
        try:
            for _e in _corr_map.get("eintraege", []):
                if _e.get("_paperless", {}).get("id") == korr_id:
                    _corr_entry = _e
                    break
        except Exception:
            pass

    # Default-Dokumenttyp aus Korrespondenten-Map als Fallback wenn LLM keinen gesetzt hat
    if corr_default_dt and not (decision.get("dokumenttyp_semantisch") or "").strip():
        log.info("Korrespondent-Default-DokTyp als Fallback: '%s'", corr_default_dt)
        decision["dokumenttyp_semantisch"] = corr_default_dt
    elif corr_default_dt:
        log.info("Korrespondent-Default-DokTyp vorhanden: '%s' (LLM hat '%s' gesetzt — behalten)",
                 corr_default_dt, decision.get("dokumenttyp_semantisch"))

    # Ausstellungsdatum (Paperless-Feld «created»)
    # Priorität: OCR-Signale (Ort und Datum, Erstellt am, …) → Vision → LLM
    import datetime as _dt
    _scan_year = _dt.date.today().year
    _birth_exclude = birth_dates_from_family(_load_family().get("personen", []))

    ocr_datum, ocr_src = extract_document_issue_date(ocr_text, _birth_exclude)
    vision_datum = vision_meta.get("datum")
    llm_datum = decision.get("datum")

    datum = None
    _datum_suspicious = False
    _datum_quelle = ""
    for candidate, quelle in [
        (ocr_datum, ocr_src or "ocr_signal"),
        (vision_datum, "vision"),
        (llm_datum, "llm"),
    ]:
        validated, suspicious = validate_issue_date(candidate, _scan_year, _birth_exclude)
        if validated:
            datum = validated
            _datum_suspicious = suspicious
            _datum_quelle = quelle
            break

    if datum:
        patch["created"] = datum
        log.info("Ausstellungsdatum: %s (Quelle: %s)", datum, _datum_quelle)
        if _datum_suspicious and decision.get("confidence") == "hoch":
            decision["confidence"] = "mittel"
            log.warning("Confidence auf 'mittel' reduziert wegen verdächtigem Datum '%s'", datum)
    elif ocr_datum or vision_datum or llm_datum:
        log.warning(
            "Ausstellungsdatum verworfen (Kandidaten ocr=%s vision=%s llm=%s)",
            ocr_datum, vision_datum, llm_datum,
        )

    # Steuerjahr — wenn Tag Steuerrelevant gesetzt (fix_tags / LLM)
    _steuerjahr: int | None = None
    _tag_names_pre = decision.get("tags") or []
    if STEUERRELEVANT_TAG in _tag_names_pre:
        from steuerjahr import infer_steuerjahr  # noqa: WPS433

        _steuerjahr = infer_steuerjahr(
            ocr_text=ocr_text,
            vision_meta=vision_meta,
            ausstellungsdatum=datum or patch.get("created"),
            doctyp_name=doctyp_name,
            title=decision.get("titel", ""),
        )
        if _steuerjahr:
            log.info("Steuerjahr: %s (Steuerrelevant)", _steuerjahr)
        else:
            log.warning("Steuerjahr nicht ermittelbar trotz Tag '%s'", STEUERRELEVANT_TAG)

    # Tags — aus LLM-Entscheidung (bereits durch Validation-Layer gefiltert)
    tag_ids = []
    for tag_name in (decision.get("tags") or []):
        tag_name = str(tag_name).strip()
        if tag_name:
            tid = resolve_tag(tag_name)
            if tid:
                tag_ids.append(tid)

    # Jahres-Tag immer aus validiertem Datum setzen (unabhängig vom LLM)
    if datum and len(str(datum)) >= 4:
        year = str(datum)[:4]
        if year.isdigit() and 2000 <= int(year) <= 2099:
            ytid = resolve_tag(year)
            if ytid and ytid not in tag_ids:
                tag_ids.append(ytid)
                log.info("Jahres-Tag automatisch gesetzt: %s", year)

    # Wenn keine Tags → wenigstens ersten erlaubten Tag aus Top-1 RAG-Treffer setzen
    if not tag_ids and allowed_tags:
        top1_tags = similar_entries[0].get("erlaubte_tags", []) if similar_entries else []
        if top1_tags:
            fallback_tag = top1_tags[0]
            ftid = resolve_tag(fallback_tag)
            if ftid:
                tag_ids.append(ftid)
                log.info("Tag-Fallback aus RAG Top-1: %s", fallback_tag)

    # ── Eskalation + Document Review Queue (Fix #4) ─────────────────────────────
    # Eskalation (Tag-Nachbesserung via llm) wenn:
    #   - keine Tags gefunden
    #   - confidence = tief
    #   - Ordner ist Fallback Familie/Sonstiges
    # Document Review Queue (pending_review Tag) zusätzlich wenn:
    #   - Korrespondent in Pending-Queue (unbekannt)
    #   - confidence != hoch  (LLM war unsicher, Mensch soll prüfen)
    #   - Ordner ist Familie/Sonstiges (Catch-All → Review nötig)
    _pfad_fuer_queue = (decision.get("ordner") or "Familie/Sonstiges").strip()
    _conf = decision.get("confidence", "tief")
    _needs_escalation = (
        not tag_ids or
        _conf == "tief" or
        _pfad_fuer_queue == "Familie/Sonstiges"
    )
    if _needs_escalation:
        grund = []
        if not tag_ids: grund.append("keine Tags")
        if _conf == "tief": grund.append("confidence=tief")
        if _pfad_fuer_queue == "Familie/Sonstiges": grund.append("Fallback-Ordner")
        log.info("Eskalation eingereiht: %s", ", ".join(grund))
        enqueue_escalation(document_id, _pfad_fuer_queue, vision_meta, ocr_text)

    # Pending-Tags — drei verschiedene je nach Grund:
    # pending_new_correspondent → unbekannter Absender (immer, unabhängig von PENDING_MODE)
    # pending_review            → LLM unsicher (confidence tief/mittel, Fallback-Ordner)
    # pending_qs                → QS-Modus aktiv (PENDING_MODE=always)

    def _add_pending_tag(tag_name: str, grund: str) -> None:
        tag_id = _get_by_name("tags", tag_name) or _create_obj("tags", tag_name)
        if tag_id and tag_id not in tag_ids:
            tag_ids.append(tag_id)
            log.info("Pending-Tag '%s' gesetzt: %s", tag_name, grund)

    # 1. Unbekannter Korrespondent → immer pending_new_correspondent
    if pending_review_needed:
        _add_pending_tag(PENDING_NEW_CORR_TAG, "Korrespondent unbekannt")
        # Auch in Document-Review-Queue — Dok bleibt sichtbar (QS + Korrespondent offen)
        enqueue_document_review(
            document_id=DOCUMENT_ID,
            pfad=decision.get("ordner", ""),
            confidence=decision.get("confidence", "mittel"),
            grund=["Korrespondent offen"],
            title=decision.get("titel", ""),
            begruendung=decision.get("begruendung", ""),
        )

    # 2. LLM unsicher → pending_review
    _llm_unsicher = (
        _conf != "hoch" or
        _pfad_fuer_queue == "Familie/Sonstiges"
    )
    if PENDING_MODE in ("always", "uncertain") and _llm_unsicher:
        _add_pending_tag(PENDING_REVIEW_TAG,
            f"confidence={_conf}" if _conf != "hoch" else "Fallback-Ordner")

    # 3. QS-Modus aktiv → pending_qs (auch wenn LLM sicher war)
    if PENDING_MODE == "always" and not _llm_unsicher and not pending_review_needed:
        _add_pending_tag(PENDING_QS_TAG, "PENDING_MODE=always")

    if _needs_pending_htr_decision:
        _add_pending_tag(PENDING_HTR_DECISION_TAG, "HTR-Entscheidung offen (Profil unsicher)")

    if tag_ids:
        patch["tags"] = list(dict.fromkeys(tag_ids))

    # Document Review Queue: bei pending_review oder pending_qs → Eintrag schreiben
    # damit paper.manager alle pending Dokumente sieht (nicht nur neue Korrespondenten)
    _has_pending_review = any(
        _get_by_name("tags", t) in tag_ids
        for t in [PENDING_REVIEW_TAG, PENDING_QS_TAG]
        if _get_by_name("tags", t)
    )
    if _has_pending_review:
        _pending_type = PENDING_REVIEW_TAG if _llm_unsicher else PENDING_QS_TAG
        _grund = []
        if _llm_unsicher: _grund.append(f"confidence={decision.get('confidence','?')}")
        if _pending_type == PENDING_QS_TAG: _grund.append("PENDING_MODE=always")
        enqueue_document_review(
            document_id=DOCUMENT_ID,
            pfad=decision.get("ordner", ""),
            confidence=decision.get("confidence", "mittel"),
            grund=_grund,
            title=decision.get("titel", ""),
            begruendung=decision.get("begruendung", ""),
        )

    # Steuerrelevant ohne Steuerjahr → Review (kein separater Schalter)
    if STEUERRELEVANT_TAG in (decision.get("tags") or []) and not _steuerjahr:
        _add_pending_tag(PENDING_REVIEW_TAG, "Steuerjahr nicht ermittelbar")
        enqueue_document_review(
            document_id=DOCUMENT_ID,
            pfad=decision.get("ordner", ""),
            confidence=decision.get("confidence", "mittel"),
            grund=["Steuerjahr nicht ermittelbar"],
            title=decision.get("titel", ""),
            begruendung=decision.get("begruendung", ""),
        )

    # Dokumenttyp — semantisch aus LLM (nicht visuell aus Vision)
    dt_name = (decision.get("dokumenttyp_semantisch") or "").strip()
    dt_id = resolve_document_type(dt_name, ocr_text=ocr_text, vision_meta=vision_meta)
    if dt_id:
        patch["document_type"] = dt_id

    # Person: Kennzeichen (family.json) schlägt Beziehung/LLM — Fahrzeugbezug vor Empfänger
    person_name = ""
    if _family_kz_match:
        person_name = _resolve_person_anzeigename(_family_kz_match[0].get("person_id", "")) or ""
        if person_name:
            _prev = (decision.get("_bez_person") or "").strip()
            if _prev and _prev != person_name:
                log.info(
                    "Person: Kennzeichen %s → %s (überschreibt %s)",
                    _family_kz_match[0].get("kennzeichen_display"), person_name, _prev,
                )
            else:
                log.info("Person aus family.json (Kennzeichen %s): %s",
                         _family_kz_match[0].get("kennzeichen_display"), person_name)
            decision["_bez_person"] = person_name

    if not person_name:
        person_name = (decision.get("_bez_person") or "").strip()

    if not person_name and _corr_entry:
        _bez_match = _match_beziehung_v2(
            _corr_entry, vision_empfaenger, ocr_text,
            dokumenttyp_visuell=vision_meta.get("dokumenttyp_visuell", ""),
            vision_meta=vision_meta,
        )
        if _bez_match:
            person_name = _resolve_person_anzeigename(_bez_match.get("person", ""))

    if not person_name:
        _direct_name, _direct_reason = _match_person_direct(ocr_text, vision_meta)
        if _direct_name:
            person_name = _direct_name
            log.info("Person direkt (%s): %s", _direct_reason, person_name)
    _will_pending_review = PENDING_MODE in ("always", "uncertain") and _llm_unsicher
    _will_pending_qs = PENDING_MODE == "always" and not _llm_unsicher and not pending_review_needed
    auto_stp = bool(
        korr_id
        and patch.get("document_type")
        and not pending_review_needed
        and not _will_pending_review
        and not _will_pending_qs
    )
    if auto_stp:
        log.info("Verarbeitung: auto STP (Korrespondent + DokTyp ohne Review)")
    if person_name:
        log.info("Person-CF: %s", person_name)

    _custom_fields = build_custom_fields(
        decision=decision,
        vision_meta=vision_meta,
        qr_meta=qr_meta,
        corr_entry=_corr_entry,
        document_id=document_id,
        person_name=person_name,
        auto_stp=auto_stp,
        steuerjahr=_steuerjahr,
        family_kennzeichen=(
            _family_kz_match[0].get("kennzeichen_display", "") if _family_kz_match else ""
        ),
    )
    if _custom_fields:
        patch["custom_fields"] = _custom_fields
        log.info("Custom Fields: %d Felder gesetzt", len(_custom_fields))

    # Storage Path
    pfad = (decision.get("ordner") or "").strip()
    if pfad and STORAGE_MODE == "api":
        sp_id = resolve_storage_path(pfad)
        if sp_id:
            patch["storage_path"] = sp_id
            log.info("Storage Path: '%s' (ID=%s)", pfad, sp_id)

    # ── Auto-Sampling VOR dem PATCH ───────────────────────────────────────────
    # Datei liegt jetzt noch unter originals/0000XXX.pdf (unstrukturiert)
    # Nach dem PATCH verschiebt Paperless sie in den strukturierten Pfad
    confidence = (decision.get("confidence") or "tief").strip().lower()
    log.info("Confidence: %s", confidence)
    doc_id_padded = str(document_id).zfill(7)
    original_pdf = f"{MEDIA_ROOT}/documents/originals/{doc_id_padded}.pdf"

    # ── Stufe-Label für Notiz ─────────────────────────────────────────────────
    _begruendung = decision.get("begruendung", "")
    if pre_decision:
        if "Kennzeichen" in _begruendung:
            _stufe_label = "0 — Kennzeichen (deterministisch)"
        elif "Beziehung" in _begruendung:
            _stufe_label = "1 — Beziehung (deterministisch)"
        elif "family.json" in _begruendung:
            _stufe_label = "2b — family.json Beziehung (deterministisch)"
        else:
            _stufe_label = "1 — deterministisch"
    else:
        _stufe_label = f"3 — LLM ({os.environ.get('OLLAMA_MODEL', 'llama3.3:70b')})"

    # ── PATCH ausführen (Paperless verschiebt Datei danach) ───────────────────
    if patch:
        ok = paperless_patch(document_id, patch)
        if ok:
            log.info("✓ PATCH erfolgreich: %s", list(patch.keys()))
        else:
            log.error("✗ PATCH fehlgeschlagen")
    else:
        log.warning("Kein Patch-Payload")

    # ── Pipeline-Notiz schreiben ─────────────────────────────────────────────
    try:
        write_pipeline_note(
            document_id       = document_id,
            decision          = decision,
            vision_meta       = vision_meta,
            pre_decision_used = pre_decision is not None,
            stufe_label       = _stufe_label,
            llm_model         = os.environ.get("OLLAMA_MODEL", "llama3.3:70b"),
        )
    except Exception as e:
        log.warning("Pipeline-Notiz fehlgeschlagen (unkritisch): %s", e)

    try:
        maybe_queue_brillenpass(
            document_id=document_id,
            ocr_text=ocr_text,
            vision_meta=vision_meta,
            corr_entry=_corr_entry,
            image_b64=image_b64,
        )
    except Exception as e:
        log.warning("Brillenpass-Queue fehlgeschlagen (unkritisch): %s", e)

    elapsed = time.monotonic() - start_time
    log.info("Fertig in %.1fs | Ordner='%s'", elapsed, pfad)
    log.info("=" * 70)



# ─── Escalation Queue ────────────────────────────────────────────────────────

def enqueue_escalation(document_id: int, pfad: str, vision_meta: dict, ocr_text: str) -> None:
    """Dokument in Eskalations-Queue schreiben (für spätere Tag-Nachbesserung)."""
    entry = {
        "document_id": document_id,
        "pfad": pfad,
        "vision_meta": vision_meta,
        "ocr_text": ocr_text[:1000],  # kompakt halten
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    ESCALATION_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with open(ESCALATION_QUEUE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log.info("Eskalation eingereiht: ID=%s Ordner='%s'", document_id, pfad)


def read_qr_meta(document_source_path: str) -> dict:
    """
    QR-Sidecar-Datei lesen die pre_consume_qr.py geschrieben hat.
    Gibt geparste Swiss QR Bill Daten zurück oder leeres Dict.
    Löscht die Sidecar-Datei nach dem Lesen (Einmal-Verwendung).
    """
    if not document_source_path:
        return {}
    sidecar = Path(document_source_path).with_suffix("").as_posix() + "_qr_meta.json"
    sidecar_path = Path(sidecar)
    if not sidecar_path.exists():
        return {}
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar_path.unlink()  # einmalig lesen, dann löschen
        if data.get("source") == "no_qr_found":
            return {}
        log.info("QR-Meta gelesen: Betrag=%s %s, Referenz=%s",
                 data.get("betrag"), data.get("waehrung"), data.get("referenz"))
        return data
    except Exception as e:
        log.warning("QR-Meta lesen fehlgeschlagen: %s", e)
        return {}


_SELECT_OPTION_CACHE: dict = {}
_KENNZEICHEN_OPTIONS_NORM: dict[str, tuple[str, str]] | None = None  # norm → (id, label)


def _kennzeichen_options_by_norm() -> dict[str, tuple[str, str]]:
    """Paperless Select-Optionen für Kennzeichen, indexiert ohne Leerzeichen/Sonderzeichen."""
    global _KENNZEICHEN_OPTIONS_NORM
    if _KENNZEICHEN_OPTIONS_NORM is not None:
        return _KENNZEICHEN_OPTIONS_NORM
    out: dict[str, tuple[str, str]] = {}
    try:
        result = _http.get(
            f"{PAPERLESS_URL}/api/custom_fields/{CF_KENNZEICHEN}/",
            headers=_headers(), timeout=15,
        ).json()
        for opt in result.get("extra_data", {}).get("select_options", []):
            label = (opt.get("label") or "").strip()
            opt_id = opt.get("id")
            if not label or not opt_id:
                continue
            norm = _norm_kz_key(label)
            if norm:
                out[norm] = (opt_id, label)
            _SELECT_OPTION_CACHE[f"{CF_KENNZEICHEN}:{label}"] = opt_id
    except Exception as e:
        log.warning("Kennzeichen-Optionen laden fehlgeschlagen: %s", e)
    _KENNZEICHEN_OPTIONS_NORM = out
    return out


def _resolve_kennzeichen_option(kennzeichen_raw: str) -> tuple[Optional[str], Optional[str]]:
    """Findet Paperless-Option per Kennzeichen — Schreibweise egal (AG178626 = AG 178 626)."""
    kz_norm = _norm_kz_key(kennzeichen_raw)
    if not kz_norm:
        return None, None
    hit = _kennzeichen_options_by_norm().get(kz_norm)
    if hit:
        return hit[0], hit[1]
    log.info(
        "Custom Field Kennzeichen: keine Option für '%s' (norm=%s) — bekannt: %s",
        kennzeichen_raw, kz_norm,
        [lbl for _, lbl in _kennzeichen_options_by_norm().values()],
    )
    return None, None


def _get_select_option_id(field_id: int, label: str) -> Optional[str]:
    """Paperless Select-Option ID für ein Label laden (gecacht).
    Select-Felder haben interne IDs wie 'hHHKXezsAIjHqkKD', nicht Label-Strings.
    """
    cache_key = f"{field_id}:{label}"
    if cache_key in _SELECT_OPTION_CACHE:
        return _SELECT_OPTION_CACHE[cache_key]
    try:
        result = _http.get(
            f"{PAPERLESS_URL}/api/custom_fields/{field_id}/",
            headers=_headers(), timeout=15
        ).json()
        extra = result.get("extra_data", {})
        options = extra.get("select_options", [])
        for opt in options:
            key = f"{field_id}:{opt.get('label','')}"
            _SELECT_OPTION_CACHE[key] = opt.get("id")
        cached = _SELECT_OPTION_CACHE.get(cache_key)
        if not cached:
            log.warning("Custom Field %d: Option '%s' nicht gefunden in %s",
                        field_id, label, [o.get('label') for o in options])
        return cached
    except Exception as e:
        log.warning("Custom Field %d Optionen laden fehlgeschlagen: %s", field_id, e)
        return None


def build_custom_fields(
    decision: dict,
    vision_meta: dict,
    qr_meta: dict,
    corr_entry: dict | None,
    *,
    document_id: int = 0,
    person_name: str = "",
    auto_stp: bool = False,
    steuerjahr: int | None = None,
    family_kennzeichen: str = "",
) -> list[dict]:
    """
    Custom Fields für Paperless PATCH zusammenstellen.
    Nur Felder setzen die wir extrahieren konnten — niemals leere Werte.

    Quellen (Priorität):
      1. QR-Meta (strukturiert, höchste Qualität)
      2. Vision-Meta (visuell extrahiert)
      3. LLM-Decision (semantisch)
      4. Korrespondenten-Muster (aus correspondents.json, zukünftig)

    Returns: Liste von {"field": ID, "value": Wert}
    """
    fields = []
    doctyp_name = (decision.get("dokumenttyp_semantisch") or "").strip()
    feldprofil = _get_feldprofil_for_doctype(doctyp_name)
    profil_active = bool(feldprofil)

    def _add(field_id: int, value, label: str, *, pipeline: bool = False):
        if not pipeline and profil_active:
            cfg = feldprofil.get(str(field_id)) or feldprofil.get(field_id) or {}
            if not cfg.get("extrahieren"):
                return
        if value is not None and str(value).strip() not in ("", "null", "None"):
            fields.append({"field": field_id, "value": value})
            log.info("Custom Field %s (%d) = %s", label, field_id, value)

    def _add_select(field_id: int, option_label: str, label: str, *, pipeline: bool = False):
        if not field_id:
            return
        opt_id = _get_select_option_id(field_id, option_label)
        if opt_id:
            _add(field_id, opt_id, f"{label}={option_label}", pipeline=pipeline)
        else:
            log.warning("Custom Field %s: Option '%s' nicht gefunden", label, option_label)

    # ── Betrag ────────────────────────────────────────────────────────────────
    # QR hat den zuverlässigsten Betrag (strukturiert)
    betrag = (
        qr_meta.get("betrag") or
        vision_meta.get("betrag") or
        decision.get("betrag")
    )
    if betrag:
        # Betrag normalisieren: "CHF 314.80" → 314.80
        import re as _re
        betrag_str = str(betrag).replace("'", "").replace(",", ".")
        betrag_clean = _re.sub(r"[^\d.]", "", betrag_str)
        if betrag_clean:
            try:
                amount = float(betrag_clean)
                if amount <= 500_000:
                    _add(CF_BETRAG, amount, "Betrag")
                else:
                    log.info("Betrag %s verworfen (unplausibel hoch — vermutlich Rechnungsnummer)", betrag)
            except ValueError:
                pass

    # ── QR-Referenz ───────────────────────────────────────────────────────────
    _add(CF_QR_REFERENZ, qr_meta.get("referenz"), "QR-Referenz")

    # ── Fällig am ─────────────────────────────────────────────────────────────
    # QR hat es manchmal strukturiert, sonst Vision
    faellig = (
        qr_meta.get("faellig_bis") or
        vision_meta.get("faellig_bis") or
        decision.get("faellig_bis")
    )
    _add(CF_FAELLIG_AM, faellig, "Fällig am")

    # ── Rechnungsnummer ───────────────────────────────────────────────────────
    rechnung_nr = (
        vision_meta.get("rechnungsnummer") or
        decision.get("rechnungsnummer")
    )
    _add(CF_RECHNUNGSNUMMER, rechnung_nr, "Rechnungsnummer")

    # ── Kundennummer ──────────────────────────────────────────────────────────
    _add(CF_KUNDENNUMMER, vision_meta.get("kundennummer"), "Kundennummer")

    # ── Policennummer ─────────────────────────────────────────────────────────
    _add(CF_POLICENNUMMER, vision_meta.get("policennummer"), "Policennummer")

    # ── Auto-Kennzeichen ──────────────────────────────────────────────────────
    kennzeichen = vision_meta.get("kennzeichen") or family_kennzeichen
    if kennzeichen:
        kz_norm_check = _norm_kz_key(kennzeichen)
        kz_map_cf = _build_kennzeichen_map()
        if kz_norm_check in kz_map_cf:
            display = kz_map_cf[kz_norm_check].get("kennzeichen_display") or kennzeichen
            kz_id, kz_label = _resolve_kennzeichen_option(display)
            if not kz_id:
                kz_id, kz_label = _resolve_kennzeichen_option(kennzeichen)
            if kz_id:
                _add(CF_KENNZEICHEN, kz_id, f"Kennzeichen={kz_label}")
        else:
            log.debug("Kennzeichen '%s' nicht in family.json — kein CF gesetzt", kennzeichen)

    # ── Status: Default "Offen" für Rechnungen — oder "Bezahlt" aus Handschrift ──
    doc_type = (decision.get("dokumenttyp_semantisch") or "").lower()
    is_rechnung = any(w in doc_type for w in ("rechnung", "abrechnung", "prämie", "invoice"))

    handschrift = vision_meta.get("handschrift") if vision_meta else None
    bezahlt_datum = parse_handschrift_bezahlt(handschrift)
    if bezahlt_datum:
        log.info("Handschrift erkannt: '%s' → bezahlt am %s", handschrift, bezahlt_datum)
        bezahlt_id = _get_select_option_id(CF_STATUS, "Bezahlt")
        if bezahlt_id:
            _add(CF_STATUS, bezahlt_id, f"Status=Bezahlt (Handschrift: {handschrift})")
        # ── Bezahlt am (ID 12) — Datum aus Handschrift ────────────────────────
        # Ermöglicht Suche: "alle Dokumente die am 06.02.2026 bezahlt wurden"
        # → Abgleich mit Onlinebanking-Zahllauf vom selben Tag
        _add(CF_BEZAHLT_AM, bezahlt_datum, f"Bezahlt am (Handschrift: {handschrift})")
    elif is_rechnung:
        offen_id = _get_select_option_id(CF_STATUS, "Offen")
        if offen_id:
            _add(CF_STATUS, offen_id, "Status=Offen")
        else:
            log.info("Custom Field Status: Option-ID für 'Offen' nicht gefunden — übersprungen")

    # ── Gescannt am (ID 13) — immer mit heutigem Datum setzen ─────────────────
    # Physisches Scan-Datum — unabhängig vom Dokumentinhalt
    import datetime as _dt2
    _add(CF_GESCANNT_AM, _dt2.date.today().isoformat(), "Gescannt am")

    # ── Pipeline-CFs (immer, unabhängig vom Feldprofil) ───────────────────────
    if CF_DOK_ID and document_id:
        _add(CF_DOK_ID, document_id, "Dok-ID", pipeline=True)
    if person_name:
        _add_select(CF_PERSON, person_name, "Person", pipeline=True)
    if auto_stp:
        _add_select(CF_VERARBEITUNG, "auto STP", "Verarbeitung", pipeline=True)
    if steuerjahr and CF_STEUERJAHR:
        _add(CF_STEUERJAHR, steuerjahr, "Steuerjahr", pipeline=True)

    return fields


AUDIT_LOG_PATH = Path(os.environ.get(
    "AUDIT_LOG_PATH",
    "/opt/paperless-scripts/training/audit_log.jsonl"
))


def write_audit_entry(
    document_id: int,
    stage: str,
    data: dict,
    violations: list = None,
) -> None:
    """Immutable Audit-Log: jede Entscheidungs-Stufe wird festgehalten.
    Ermöglicht später: "warum wurde Dok #X so klassifiziert?"

    Stages: vision, llm_raw, sanitized, final_patch, review
    """
    entry = {
        "ts":          time.strftime("%Y-%m-%dT%H:%M:%S"),
        "document_id": document_id,
        "stage":       stage,
        "data":        data,
        "violations":  violations or [],
    }
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("Audit-Log schreiben fehlgeschlagen: %s", e)


def enqueue_document_review(document_id: int, pfad: str, confidence: str,
                             grund: list, title: str = "",
                             begruendung: str = "") -> None:
    """Dokument in Document Review Queue schreiben.
    Wird befüllt wenn: confidence!=hoch, Familie/Sonstiges, had_violations, neuer Korrespondent.
    Der Correspondent Manager zeigt diese Queue zur manuellen Nachkontrolle an.

    Dedupe: document_id ist unique in pending-Einträgen.
    Bei re-consume / retry wird der bestehende Eintrag aktualisiert statt dupliziert.
    """
    import fcntl as _fcntl
    DOCUMENT_REVIEW_QUEUE.parent.mkdir(parents=True, exist_ok=True)

    lock_path = DOCUMENT_REVIEW_QUEUE.parent / ".document_review_queue.lock"
    lock_fd = open(lock_path, "w")
    try:
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX)

        # Bestehende Einträge lesen
        existing = []
        if DOCUMENT_REVIEW_QUEUE.exists():
            with open(DOCUMENT_REVIEW_QUEUE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            existing.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

        def _merge_grund(existing_g, new_g):
            return list(dict.fromkeys([*(existing_g or []), *(new_g or [])]))

        # Fall 1: doppelte pending-Zeilen pro doc_id zusammenführen
        pending_by_doc: dict[int, dict] = {}
        other_entries: list[dict] = []
        for e in existing:
            if e.get("status") != "pending":
                other_entries.append(e)
                continue
            try:
                did = int(e.get("document_id"))
            except (TypeError, ValueError):
                other_entries.append(e)
                continue
            if did not in pending_by_doc:
                pending_by_doc[did] = dict(e)
                pending_by_doc[did]["document_id"] = did
            else:
                base = pending_by_doc[did]
                base["grund"] = _merge_grund(base.get("grund"), e.get("grund"))
                if not base.get("pfad"):
                    base["pfad"] = e.get("pfad", "")
                if not base.get("title"):
                    base["title"] = e.get("title", "")
        existing = other_entries + list(pending_by_doc.values())

        try:
            doc_id_int = int(document_id)
        except (TypeError, ValueError):
            doc_id_int = document_id

        # Dedupe: pending oder approved→pending reaktivieren
        updated = False
        now_ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        for e in existing:
            try:
                eid = int(e.get("document_id"))
            except (TypeError, ValueError):
                continue
            if eid != doc_id_int:
                continue
            if e.get("status") == "pending":
                e["pfad"] = pfad or e.get("pfad", "")
                e["confidence"] = confidence or e.get("confidence", "mittel")
                e["grund"] = _merge_grund(e.get("grund"), grund)
                e["title"] = title or e.get("title", "")
                if begruendung:
                    old = (e.get("begruendung") or "").strip()
                    e["begruendung"] = begruendung if not old else (
                        begruendung if begruendung in old else f"{old}\n{begruendung}".strip()
                    )
                e["timestamp"] = now_ts
                updated = True
                log.info("Document Review Queue: ID=%s aktualisiert (Dedupe)", document_id)
                break
            if e.get("status") == "approved":
                e["status"] = "pending"
                e.pop("reviewed_at", None)
                e["pfad"] = pfad or e.get("pfad", "")
                e["confidence"] = confidence or e.get("confidence", "mittel")
                e["grund"] = _merge_grund(e.get("grund"), grund)
                e["title"] = title or e.get("title", "")
                e["begruendung"] = begruendung or e.get("begruendung", "")
                e["timestamp"] = now_ts
                updated = True
                log.info("Document Review Queue: ID=%s reaktiviert (approved→pending)", document_id)
                break

        if not updated:
            existing.append({
                "document_id":  doc_id_int,
                "pfad":         pfad,
                "confidence":   confidence,
                "grund":        grund,
                "title":        title,
                "begruendung":  begruendung,
                "status":       "pending",
                "timestamp":    time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            log.info("Document Review Queue: ID=%s neu eingereiht, Grund=%s", document_id, grund)

        # Atomar zurückschreiben
        with open(DOCUMENT_REVIEW_QUEUE, "w", encoding="utf-8") as f:
            for e in existing:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    finally:
        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
        lock_fd.close()


def save_correction(doc_id: int, pfad: str, tags_vorher: list, tags_nachher: list, grund: str) -> None:
    """Korrektur in corrections.jsonl schreiben.
    Idempotent: gleiche doc_id + gleicher Grund wird nicht doppelt gespeichert.
    """
    entry = {
        "document_id": doc_id,
        "vorher": f"{pfad} tags={tags_vorher}",
        "nachher": f"{pfad} tags={tags_nachher}",
        "grund": grund,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    CORRECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CORRECTIONS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def update_manifest_tags(pfad: str, new_tags: list[str]) -> None:
    """Neue Tags in erlaubte_tags des Manifest-Eintrags schreiben und speichern."""
    if not MANIFEST_PATH.exists():
        return
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            manifest_data = json.load(f)
        changed = False
        for entry in manifest_data.get("ordner", []):
            if entry.get("pfad") == pfad:
                existing = set(entry.get("erlaubte_tags", []))
                added = [t for t in new_tags if t not in existing]
                if added:
                    entry["erlaubte_tags"] = sorted(existing | set(new_tags))
                    log.info("Manifest '%s': neue Tags ergänzt: %s", pfad, added)
                    changed = True
                break
        if changed:
            with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
                json.dump(manifest_data, f, ensure_ascii=False, indent=2)
            # Embedding-Cache invalidieren
            cache_path = MANIFEST_PATH.parent / "manifest_embeddings.json"
            if cache_path.exists():
                cache_path.unlink()
                log.info("Embedding-Cache gelöscht (Manifest geändert)")
    except Exception as e:
        log.warning("Manifest-Update fehlgeschlagen: %s", e)


def process_escalation_queue() -> None:
    """
    Alle Einträge in der Escalation-Queue mit qwen3:32b nachbearbeiten.
    Läuft nur wenn kein anderer post_consume Prozess mehr aktiv ist.
    Tags die gefunden werden: PATCH + Manifest-Update + Korrektur schreiben.
    """
    if not ESCALATION_QUEUE.exists():
        return

    # Prozess-Check via PID-Registry (wird von __main__ sichergestellt)
    # Hier kein zusätzlicher Check nötig — __main__ prüft bereits is_last_consumer()

    # Queue lesen
    entries = []
    with open(ESCALATION_QUEUE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if not entries:
        ESCALATION_QUEUE.unlink(missing_ok=True)
        return

    log.info("=" * 70)
    log.info("ESKALATION: %d Dokumente mit %s nachbearbeiten", len(entries), os.environ.get("OLLAMA_MODEL_ESCALATION", "llama3.3:70b"))

    manifest = load_manifest()
    # Standard: llama3.3:70b statt qwen3:32b — qwen3 Thinking-Modus stoert JSON-Extraktion
    # Ueberschreibbar via: OLLAMA_MODEL_ESCALATION=qwen3:32b
    escalation_model = os.environ.get("OLLAMA_MODEL_ESCALATION", "llama3.3:70b")

    for entry in entries:
        doc_id  = entry["document_id"]
        pfad    = entry["pfad"]
        vm      = entry.get("vision_meta", {})
        ocr     = entry.get("ocr_text", "")

        log.info("Eskalation ID=%s Ordner='%s'", doc_id, pfad)

        # Constraints für diesen Ordner
        ordner_entry = next((e for e in manifest if e.get("pfad") == pfad), {})
        allowed = set(ordner_entry.get("erlaubte_tags", []))
        verboten = set(ordner_entry.get("verbotene_tags", []))
        allowed -= verboten
        max_t = ordner_entry.get("max_tags", 4)

        allowed_str = ", ".join(sorted(allowed)) if allowed else "(keine Einschränkung)"

        escalation_prompt = f"""Du bist ein Dokumenten-Klassifikator. Das Dokument wurde bereits dem Ordner '{pfad}' zugeordnet.
Deine Aufgabe: Bestimme passende Tags für dieses Dokument.

VISUELLE ANALYSE:
{json.dumps(vm, ensure_ascii=False)}

OCR-TEXT:
{ocr}

ERLAUBTE TAGS (AUSSCHLIESSLICH aus dieser Liste!):
{allowed_str}

VERBOTENE TAGS: {", ".join(sorted(verboten)) if verboten else "(keine)"}
MAXIMALE TAGS: {max_t}

Antworte NUR mit JSON:
{{"tags": ["Tag1", "Tag2"], "dokumenttyp_semantisch": "Typ", "begruendung": "1 Satz"}}"""

        try:
            resp = ollama_post(
                "api/chat",
                {
                    "model": escalation_model,
                    "messages": [{"role": "user", "content": escalation_prompt}],
                    "system": "Antworte ausschliesslich mit einem validen JSON-Objekt. Kein Markdown.",
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.1, "num_predict": 256},
                },
                timeout=LLM_TIMEOUT,
            )
            raw = resp.get("message", {}).get("content", "")
            esc_decision = extract_json_from_response(raw)
        except Exception as e:
            log.warning("Eskalation ID=%s fehlgeschlagen: %s", doc_id, e)
            continue

        if not esc_decision:
            continue
        esc_decision = normalize_decision_keys(esc_decision)

        # Tags validieren
        raw_tags = esc_decision.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        elif not isinstance(raw_tags, list):
            raw_tags = []

        validated = [t for t in raw_tags if (not allowed or t in allowed) and t not in verboten]
        validated = validated[:max_t]

        if not validated:
            log.info("Eskalation ID=%s: keine validen Tags gefunden", doc_id)
            continue

        # PATCH: nur Tags + Dokumenttyp — ADDITIV (bestehende Werte erhalten!)
        # Paperless PATCH überschreibt Tags komplett → bestehende IDs vorher lesen
        patch: dict = {}

        # ── Bestehende Tags des Dokuments lesen ──────────────────────────────
        existing_tag_ids: list = []
        try:
            doc_current = _http.get(
                f"{PAPERLESS_URL}/api/documents/{doc_id}/",
                headers=_headers(), timeout=15
            ).json()
            existing_tag_ids = doc_current.get("tags", [])
            log.info("Eskalation ID=%s: bestehende Tags=%s", doc_id, existing_tag_ids)
        except Exception as e:
            log.warning("Eskalation ID=%s: bestehende Tags nicht lesbar: %s — Abbruch (kein PATCH)", doc_id, e)
            continue  # Sicherheit: lieber nichts tun als Tags überschreiben

        # Neue Tag-IDs auflösen
        new_tag_ids = []
        for tag_name in validated:
            tid = resolve_tag(tag_name)
            if tid:
                new_tag_ids.append(tid)

        # Merge: bestehende + neue, dedupliziert
        merged_tag_ids = list(dict.fromkeys(existing_tag_ids + new_tag_ids))
        if merged_tag_ids != existing_tag_ids:
            patch["tags"] = merged_tag_ids
            log.info("Eskalation ID=%s: Tags merged: %s + %s → %s",
                     doc_id, existing_tag_ids, new_tag_ids, merged_tag_ids)
        else:
            log.info("Eskalation ID=%s: keine neuen Tags — PATCH wird minimal gehalten", doc_id)

        # Dokumenttyp NUR setzen wenn noch keiner gesetzt ist
        dt = (esc_decision.get("dokumenttyp_semantisch") or "").strip()
        if dt:
            existing_dt = doc_current.get("document_type")
            if not existing_dt:  # nur setzen wenn leer
                dtid = resolve_document_type(dt)
                if dtid:
                    patch["document_type"] = dtid
            else:
                log.info("Eskalation ID=%s: Dokumenttyp bereits gesetzt (%s) — nicht überschreiben", doc_id, existing_dt)

        # NIEMALS storage_path, correspondent, title im Eskalations-PATCH anfassen!
        # Owner + Permissions immer mitschicken via zentrale Funktion
        patch.update(_default_permissions())

        if patch:
            ok = paperless_patch(doc_id, patch)
            if ok:
                log.info("Eskalation ID=%s PATCH erfolgreich: Tags=%s", doc_id, validated)
                # Korrektur speichern (für RAG-Kontext beim nächsten Lauf)
                save_correction(doc_id, pfad, [], validated, esc_decision.get("begruendung", "Eskalation"))
                # Manifest NICHT automatisch updaten — verhindert Tag-Drift / Ontologie-Verschmutzung
                # Manuelle Überprüfung nötig: update_manifest_tags(pfad, validated)
                log.info("Eskalation: Manifest-Update übersprungen (manuelle Bestätigung nötig)")
            else:
                log.error("Eskalation ID=%s PATCH fehlgeschlagen", doc_id)

    # Queue leeren
    ESCALATION_QUEUE.unlink(missing_ok=True)
    log.info("Eskalation abgeschlossen")
    log.info("=" * 70)

# ─── Pipeline-Lock (pre_consume + post_consume teilen sich dieselbe Datei) ───
PIPELINE_LOCK = Path("/tmp/paperless_consume_pipeline.lock")


def _acquire_pipeline_lock():
    """Exklusiver Lock — verhindert parallele OCR/LLM-Läufe bei mehreren Celery-Workern."""
    import fcntl
    fd = open(PIPELINE_LOCK, "w")
    log.info("Pipeline-Lock: warte (PID %s, Dok %s)...", os.getpid(), DOCUMENT_ID or "?")
    fcntl.flock(fd, fcntl.LOCK_EX)
    log.info("Pipeline-Lock: erhalten")
    return fd


def _release_pipeline_lock(fd) -> None:
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        fd.close()
    log.info("Pipeline-Lock: freigegeben")


# ─── PID-Registry für Prozess-Koordination ──────────────────────────────────
PID_REGISTRY   = Path("/tmp/paperless_consume.pids")
ESCALATION_LOCK = Path("/tmp/paperless_escalation.lock")


def _pid_locked_update(add_pid: int = None, remove_pid: int = None) -> set:
    """
    Atomar PIDs lesen/schreiben via flock.
    Tote Prozesse werden automatisch bereinigt.
    """
    import fcntl
    PID_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    # Öffne im read/write Modus, erstelle falls nötig
    fd = open(PID_REGISTRY, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # exklusives Lock
        fd.seek(0)
        alive = set()
        for line in fd.read().splitlines():
            try:
                pid = int(line.strip())
                os.kill(pid, 0)  # Signal 0 = Prozess existiert noch?
                alive.add(pid)
            except (ValueError, OSError):
                pass  # tot oder ungültig → ignorieren
        if add_pid:
            alive.add(add_pid)
        if remove_pid:
            alive.discard(remove_pid)
        # Atomar zurückschreiben
        fd.seek(0)
        fd.truncate()
        fd.write("\n".join(str(p) for p in alive))
        fd.flush()
        return alive
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def register_pid() -> None:
    """Eigene PID atomar in Registry eintragen."""
    _pid_locked_update(add_pid=os.getpid())


def unregister_pid() -> set:
    """Eigene PID atomar aus Registry entfernen. Gibt verbleibende PIDs zurück."""
    return _pid_locked_update(remove_pid=os.getpid())


def _read_pids() -> set:
    """PIDs lesen (ohne Schreiben) — für is_last_consumer_check."""
    return _pid_locked_update()  # kein add/remove → nur lesen + bereinigen


def is_last_consumer() -> bool:
    """True wenn keine anderen post_consume Prozesse mehr laufen."""
    return len(_read_pids()) == 0


if __name__ == "__main__":
    register_pid()
    try:
        # Legacy: kein Pipeline-Lock, keine Vision/LLM — nur Tag-Fallback
        marker_path = _take_legacy_marker_path()
        if _is_legacy_import(marker_path):
            log.info(
                "Legacy-Import — Pipeline übersprungen (source=%s, original=%s, tags=%s, marker=%s)",
                DOCUMENT_SOURCE_PATH or "-",
                os.environ.get("DOCUMENT_ORIGINAL_FILENAME", "-"),
                os.environ.get("DOCUMENT_TAGS", "-"),
                marker_path or "-",
            )
            if DOCUMENT_ID and PAPERLESS_TOKEN:
                _finalize_legacy_import(int(DOCUMENT_ID), marker_path)
            sys.exit(0)

        lock_fd = _acquire_pipeline_lock()
        try:
            main()
        finally:
            _release_pipeline_lock(lock_fd)
    except Exception:
        log.critical("Unbehandelter Fehler:\n%s", traceback.format_exc())
        if DOCUMENT_ID and PAPERLESS_TOKEN:
            try:
                ensure_dok_id(int(DOCUMENT_ID))
            except Exception as _e:
                log.warning("ensure_dok_id im Fehlerfall fehlgeschlagen: %s", _e)
    finally:
        # Prüfen ob wir der letzte sind BEVOR wir uns deregistrieren
        # remaining = alle anderen noch aktiven Prozesse (wir selbst ausgenommen)
        remaining = unregister_pid()

        if not remaining and ESCALATION_QUEUE.exists():
            # Atomares Lock — verhindert Doppelstart bei exakt gleichzeitigem Finish
            try:
                fd = os.open(str(ESCALATION_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
            except FileExistsError:
                log.info("Eskalation: anderer Prozess hat Lock — überspringe")
                sys.exit(0)
            try:
                process_escalation_queue()
            finally:
                ESCALATION_LOCK.unlink(missing_ok=True)
        sys.exit(0)