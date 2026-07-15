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
        # Grobe Filter: Bank-Codes enden oft auf «XX» (Ländercode)
        if n[4:6].isalpha() and n[:4].isalpha():
            seen.add(n)
            found.append(n)
        if len(found) >= max_results:
            break

    return found[:max_results]
