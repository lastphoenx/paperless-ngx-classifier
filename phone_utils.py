"""Telefon-Extraktion und -Normalisierung (phonenumbers mit Regex-Fallback)."""
from __future__ import annotations

import re

try:
    import phonenumbers
    from phonenumbers import PhoneNumberFormat

    _HAS_PHONENUMBERS = True
except ImportError:  # pragma: no cover — Tests laufen mit/ohne Lib
    phonenumbers = None  # type: ignore[assignment]
    PhoneNumberFormat = None  # type: ignore[assignment,misc]
    _HAS_PHONENUMBERS = False

# Schweiz + häufige Nachbarländer in CH-Dokumenten
_DEFAULT_REGIONS = ("CH", "DE", "AT", "IT", "FR", "LI")

_TEL_LABEL_RE = re.compile(
    r"(?:Telefon|Tel\.?|Phone|Mobile|Mobil|Handy|Direkt|Zentrale|Fax|Telefax)"
    r"\s*[:\s]*([+()\d][\d\s()./\-]{6,32})",
    re.IGNORECASE,
)
_CH_INTL_RE = re.compile(
    r"(?:\+41|0041)\s*[\-]?\s*(?:\(\s*0\s*\)\s*)?(?:\d{2}|\d{3})"
    r"[\s.\-]?\d{3}[\s.\-]?\d{2}[\s.\-]?\d{2}\b",
)
_CH_NATIONAL_RE = re.compile(
    r"\b0[1-9]\d[\s.\-/]?\d{3}[\s.\-/]?\d{2}[\s.\-/]?\d{2}\b",
)


def preprocess_phone_text(text: str) -> str:
    """+41 (0) 61 … → +41 61 … (häufig auf Rechnungen)."""
    s = text or ""
    s = re.sub(r"(?i)(\+41|0041)\s*\(\s*0\s*\)\s*", r"\1 ", s)
    return s


def norm_phone_for_match(raw: str, default_region: str = "CH") -> str:
    """
    E.164-Ziffern ohne «+» für Substring-Match im Dokument (z. B. 41619718980).
    Fallback: bisherige CH-Heuristik wenn phonenumbers fehlt oder Parse scheitert.
    """
    s = preprocess_phone_text((raw or "").strip())
    if not s:
        return ""

    if _HAS_PHONENUMBERS:
        for region in (default_region, *_DEFAULT_REGIONS):
            try:
                parsed = phonenumbers.parse(s, region)
            except phonenumbers.NumberParseException:
                continue
            if phonenumbers.is_possible_number(parsed):
                return phonenumbers.format_number(parsed, PhoneNumberFormat.E164).lstrip("+")

    digits = re.sub(r"\D", "", s)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("41") and len(digits) >= 11:
        return digits
    if digits.startswith("0") and len(digits) >= 10:
        return "41" + digits[1:]
    return digits


def _phone_display(raw: str, default_region: str = "CH") -> str | None:
    s = preprocess_phone_text((raw or "").strip())
    if not s:
        return None
    if _HAS_PHONENUMBERS:
        for region in (default_region, *_DEFAULT_REGIONS):
            try:
                parsed = phonenumbers.parse(s, region)
            except phonenumbers.NumberParseException:
                continue
            if phonenumbers.is_possible_number(parsed):
                return phonenumbers.format_number(parsed, PhoneNumberFormat.INTERNATIONAL)
    cleaned = re.sub(r"\s+", " ", s.strip().rstrip(" -"))
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 9:
        return None
    return cleaned


def _add_phone(
    raw: str,
    out: list[str],
    seen: set[str],
    *,
    default_region: str = "CH",
) -> None:
    display = _phone_display(raw, default_region=default_region)
    if not display:
        return
    key = norm_phone_for_match(display, default_region=default_region)
    if not key or len(key) < 9 or key in seen:
        return
    seen.add(key)
    out.append(display)


def extract_phones_from_text(
    text: str,
    *,
    max_results: int = 3,
    default_region: str = "CH",
) -> list[str]:
    """Telefonnummern aus OCR/Vision-Text — Labels, phonenumbers-Matcher, CH-Regex."""
    if not (text or "").strip():
        return []

    prepared = preprocess_phone_text(text)
    found: list[str] = []
    seen: set[str] = set()

    for m in _TEL_LABEL_RE.findall(prepared):
        _add_phone(m, found, seen, default_region=default_region)
        if len(found) >= max_results:
            return found[:max_results]

    if _HAS_PHONENUMBERS:
        for region in _DEFAULT_REGIONS:
            try:
                for match in phonenumbers.PhoneNumberMatcher(prepared, region):
                    _add_phone(match.raw_string, found, seen, default_region=region)
                    if len(found) >= max_results:
                        return found[:max_results]
            except Exception:
                continue

    for pat in (_CH_INTL_RE, _CH_NATIONAL_RE):
        for m in pat.findall(prepared):
            _add_phone(m, found, seen, default_region=default_region)
            if len(found) >= max_results:
                return found[:max_results]

    return found[:max_results]
