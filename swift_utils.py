"""SWIFT/BIC-Extraktion für Bank-Korrespondenten."""
from __future__ import annotations

import re

_SWIFT_LABEL_RE = re.compile(
    r"(?:SWIFT|BIC)\s*(?:Code)?\s*[:\s]*([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)",
    re.IGNORECASE,
)
_SWIFT_STANDALONE_RE = re.compile(
    r"\b([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b",
)

# Standalone-Regex trifft oft deutsche Rechnungswörter — nur plausible Ländercodes.
_SWIFT_COUNTRY_WHITELIST = frozenset({
    "AD", "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR",
    "GB", "GI", "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MC",
    "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK", "SM", "US",
})

# Bekannte False Positives aus OCR/Fliesstext (8–11 Zeichen, sonst SWIFT-ähnlich).
_SWIFT_WORD_DENYLIST = frozenset({
    "BESTELLUNG", "MARKETPLACE", "RECHNUNG", "UNZUFRIEDEN", "KUNDENSERVICE",
    "ZAHLUNGSZIEL", "LIEFERANT", "HANDELSREGISTER", "MWSTNUMMER",
})


def normalize_swift(raw: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "", (raw or "").upper())
    if len(s) in (8, 11) and s[:4].isalpha():
        return s
    return ""


def extract_swifts_from_text(text: str, *, max_results: int = 2) -> list[str]:
    if not (text or "").strip():
        return []
    found: list[str] = []
    seen: set[str] = set()

    for m in _SWIFT_LABEL_RE.findall(text):
        n = normalize_swift(m)
        if n and n not in seen:
            seen.add(n)
            found.append(n)
        if len(found) >= max_results:
            return found

    for m in _SWIFT_STANDALONE_RE.findall(text.upper()):
        n = normalize_swift(m)
        if not n or n in seen:
            continue
        if n in _SWIFT_WORD_DENYLIST:
            continue
        country = n[4:6]
        if not (n[:4].isalpha() and country.isalpha() and country in _SWIFT_COUNTRY_WHITELIST):
            continue
        seen.add(n)
        found.append(n)
        if len(found) >= max_results:
            break

    return found[:max_results]
