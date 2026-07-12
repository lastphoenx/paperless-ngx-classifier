"""IBAN-Normalisierung, Extraktion und Validierung (Modulo 97 + Länderlänge).

Keine externe Abhängigkeit — filtert OCR-Falschpositive wie «CHRISTODERVACCINATION».
"""
from __future__ import annotations

import re

# ISO 13616 Längen (häufige Länder in CH/DE/AT-Kontext)
IBAN_LENGTHS: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16,
    "BG": 22, "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28,
    "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24,
    "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18,
    "GR": 27, "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IS": 26,
    "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32, "LI": 21,
    "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19,
    "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24, "PL": 28,
    "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24, "SE": 24,
    "SI": 19, "SK": 24, "SM": 27, "TN": 24, "TR": 26, "UA": 29, "VA": 22,
    "VG": 24, "XK": 20,
}

# Keine Capture-Groups — finditer liefert stabile Volltreffer
_SPACED_IBAN_RE = re.compile(
    r"\b[A-Z]{2}\d{2}(?:[\s\-]?[0-9A-Z]{4}){1,7}[\s\-]?[0-9A-Z]{0,4}\b",
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(r"[A-Z]{2}\d{2}")


def normalize_iban(raw: str) -> str:
    """Kompakt, Grossbuchstaben, ohne Leerzeichen/Bindestriche."""
    return re.sub(r"[\s\-]", "", str(raw or "").upper())


def fix_iban_ocr_compact(compact: str) -> str:
    """Häufiger OCR-Fehler: CHO… → CH0… (compact muss bereits normalisiert sein)."""
    if compact.startswith("CHO") and len(compact) >= 4:
        return "CH0" + compact[3:]
    return compact


def is_valid_iban_compact(compact: str) -> bool:
    """Modulo 97 + Länge — compact muss bereits normalisiert sein."""
    iban = compact
    if len(iban) < 15:
        return False
    country = iban[:2]
    expected = IBAN_LENGTHS.get(country)
    if not expected or len(iban) != expected:
        return False
    if not re.fullmatch(r"[A-Z0-9]+", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    number_str = "".join(
        str(ord(ch) - ord("A") + 10) if ch.isalpha() else ch
        for ch in rearranged
    )
    try:
        return int(number_str) % 97 == 1
    except ValueError:
        return False


def is_valid_iban(raw: str) -> bool:
    """Prüfziffer Modulo 97 + exakte Länderlänge (beliebiges Eingabeformat)."""
    return is_valid_iban_compact(normalize_iban(raw))


def validate_iban(raw: str) -> str | None:
    """Normalisiert und gibt kompakte IBAN zurück, oder None wenn ungültig."""
    compact = normalize_iban(raw)
    if is_valid_iban_compact(compact):
        return compact
    fixed = fix_iban_ocr_compact(compact)
    if fixed != compact and is_valid_iban_compact(fixed):
        return fixed
    return None


def format_iban_display(compact: str) -> str:
    """Gruppiert eine bereits validierte kompakte IBAN (4er-Blöcke)."""
    if not compact:
        return ""
    parts = [compact[i : i + 4] for i in range(0, len(compact), 4)]
    return " ".join(parts)


def _candidates_from_compact(text: str) -> list[str]:
    compact = re.sub(r"\s+", "", text or "").upper()
    if not compact:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _ANCHOR_RE.finditer(compact):
        country = compact[m.start() : m.start() + 2]
        length = IBAN_LENGTHS.get(country)
        if not length:
            continue
        candidate = compact[m.start() : m.start() + length]
        if len(candidate) != length:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def extract_ibans_from_text(text: str, *, max_results: int = 3) -> list[str]:
    """Gültige IBANs aus OCR/QR-Text — nur nach Modulo-97-Prüfung."""
    found: list[str] = []
    seen: set[str] = set()

    def _add_raw(raw: str) -> None:
        if len(found) >= max_results:
            return
        valid = validate_iban(raw)
        if valid and valid not in seen:
            seen.add(valid)
            found.append(format_iban_display(valid))

    for m in _SPACED_IBAN_RE.finditer(text or ""):
        _add_raw(m.group(0))

    for candidate in _candidates_from_compact(text or ""):
        _add_raw(candidate)

    return found
