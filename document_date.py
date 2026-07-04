"""
Generische Ausstellungsdatum-Extraktion aus OCR-Text.

Keine hardcodierten Orte — nur Signale wie «Ort und Datum», «Erstellt am», «Datum …».
"""
from __future__ import annotations

import re
from datetime import datetime

_MONTH_GROUPS = [
    ("januar", "january", "jan", "gennaio", "janvier"),
    ("februar", "february", "feb", "febbraio", "février", "fevrier"),
    ("märz", "marz", "march", "mar", "marzo", "mars"),
    ("april", "apr", "avril"),
    ("mai", "may", "maggio"),
    ("juni", "june", "jun", "giugno", "juin"),
    ("juli", "july", "jul", "luglio", "juillet"),
    ("august", "aug", "août", "aout"),
    ("september", "sep", "sept", "settembre"),
    ("oktober", "october", "oct", "ottobre", "octobre"),
    ("november", "nov", "novembre"),
    ("dezember", "december", "dec", "dicembre", "décembre", "decembre"),
]
_MONTH_MAP: dict[str, int] = {}
for _idx, _names in enumerate(_MONTH_GROUPS, start=1):
    for _n in _names:
        _MONTH_MAP[_n.lower()] = _idx

_MONTH_PATTERN = "|".join(re.escape(n) for names in _MONTH_GROUPS for n in names)

_DATE_NUMERIC = r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})"
_DATE_MONTH_NAME = rf"(\d{{1,2}})[.\s]+({_MONTH_PATTERN})\s+(\d{{4}})"
_DATE_MONTH_NAME2 = rf"(\d{{1,2}})\s+({_MONTH_PATTERN})\s+(\d{{4}})"


def _to_iso(d: int, mo: int, y: int) -> str | None:
    if y < 100:
        y = 2000 + y if y < 70 else 1900 + y
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_numeric(m: re.Match) -> str | None:
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return _to_iso(d, mo, y)


def _parse_month_name(m: re.Match) -> str | None:
    d = int(m.group(1))
    mo = _MONTH_MAP.get(m.group(2).lower().replace("é", "e").replace("û", "u"))
    if not mo:
        return None
    y = int(m.group(3))
    return _to_iso(d, mo, y)


def _find_date_after(text: str, start: int, window: int = 120) -> tuple[str | None, str]:
    """Erstes Datum in text[start:start+window]."""
    chunk = text[start : start + window]
    for pat, parser in [
        (_DATE_NUMERIC, _parse_numeric),
        (_DATE_MONTH_NAME, _parse_month_name),
        (_DATE_MONTH_NAME2, _parse_month_name),
    ]:
        m = re.search(pat, chunk, re.IGNORECASE)
        if m:
            iso = parser(m)
            if iso:
                return iso, m.group(0)
    return None, ""


def extract_document_issue_date(
    ocr_text: str,
    exclude_iso_dates: set[str] | None = None,
) -> tuple[str | None, str]:
    """
    Ausstellungsdatum aus typischen CH/DE-Formulierungen.
    Returns: (YYYY-MM-DD, quelle) — quelle für Logging.
    """
    text = ocr_text or ""
    if not text.strip():
        return None, ""
    exclude = exclude_iso_dates or set()
    lower = text.lower()

    # 1. «Ort und Datum» / «Luogo e data» / «Lieu et date»
    for label in (
        r"ort\s+und\s+datum",
        r"lieu\s+et\s+date",
        r"luogo\s+e\s+data",
    ):
        for m in re.finditer(label, lower, re.IGNORECASE):
            iso, _ = _find_date_after(text, m.end(), 150)
            if iso and iso not in exclude:
                return iso, "signal:ort_und_datum"

    # 2. «Erstellt am» / «Ausgestellt am» / «Fait à» / «Emesso il»
    for label in (
        r"erstellt\s+am",
        r"ausgestellt\s+am",
        r"ausgefertigt\s+am",
        r"datum\s*:",
        r"fait\s+[àa]",
        r"emesso\s+il",
    ):
        for m in re.finditer(label, lower, re.IGNORECASE):
            iso, _ = _find_date_after(text, m.end(), 80)
            if iso and iso not in exclude:
                return iso, f"signal:{label}"

    # 3. «Datum» + Ort + Datum (z. B. «Datum Aarau, 7. November 2025»)
    for m in re.finditer(r"\bdatum\b", lower, re.IGNORECASE):
        iso, _ = _find_date_after(text, m.end(), 100)
        if iso and iso not in exclude:
            return iso, "signal:datum"

    # 4. «Stadt, den DD.MM.YYYY» / «Stadt, DD.MM.YYYY» (generisch: Wort + Komma)
    for m in re.finditer(
        r"([A-ZÀ-Ü][A-Za-zÀ-ÿ\-\.]+),\s*(?:den\s+)?" + _DATE_NUMERIC,
        text,
    ):
        iso = _parse_numeric(m)
        if iso and iso not in exclude:
            return iso, "signal:ort_komma_datum"

    # 5. «Stadt, den DD. Monat YYYY»
    for m in re.finditer(
        r"([A-ZÀ-Ü][A-Za-zÀ-ÿ\-\.]+),\s*(?:den\s+)?" + _DATE_MONTH_NAME,
        text,
        re.IGNORECASE,
    ):
        iso = _parse_month_name(m)
        if iso and iso not in exclude:
            return iso, "signal:ort_komma_monat"

    # 6. Fusszeile: letzte 2000 Zeichen — oft Unterschrift / Ausstellungsdatum
    tail = text[-2000:]
    candidates: list[tuple[str, str, int]] = []
    for pat, parser in [
        (_DATE_NUMERIC, _parse_numeric),
        (_DATE_MONTH_NAME, _parse_month_name),
        (_DATE_MONTH_NAME2, _parse_month_name),
    ]:
        for m in re.finditer(pat, tail, re.IGNORECASE):
            iso = parser(m)
            if iso and iso not in exclude:
                candidates.append((iso, "signal:fusszeile", m.start()))

    if candidates:
        candidates.sort(key=lambda x: x[2], reverse=True)
        return candidates[0][0], candidates[0][1]

    return None, ""


def birth_dates_from_family(personen: list[dict]) -> set[str]:
    """Geburtsdaten als ISO-Set — nicht als Ausstellungsdatum verwenden."""
    out: set[str] = set()
    for p in personen or []:
        geb = (p.get("geburtsdatum") or "").strip()
        if not geb:
            continue
        m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$", geb)
        if m:
            iso = _to_iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if iso:
                out.add(iso)
        m2 = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", geb)
        if m2:
            out.add(geb)
    return out


def validate_issue_date(
    candidate: str | None,
    scan_year: int,
    exclude_iso_dates: set[str] | None = None,
) -> tuple[str | None, bool]:
    """
    Plausibilität: Jahr 1990–scan_year+1, nicht in exclude-Set.
    Returns: (iso_or_none, suspicious_if_old).
    """
    if not candidate or str(candidate).lower() in ("null", "none", ""):
        return None, False
    exclude = exclude_iso_dates or set()
    try:
        raw = str(candidate).strip()[:10]
        if len(raw) >= 10 and raw[4] == "-":
            iso = raw
        else:
            m = re.match(_DATE_NUMERIC, raw)
            if not m:
                return None, False
            iso = _parse_numeric(m)
        if not iso or iso in exclude:
            return None, False
        year = int(iso[:4])
        if not (1990 <= year <= scan_year + 1):
            return None, False
        suspicious = (scan_year - year) > 2
        return iso, suspicious
    except (ValueError, TypeError):
        return None, False


DATUM_PROMPT_HINT = (
    "datum = Ausstellungsdatum des Dokuments (nicht Geburtsdatum, nicht Vertragsbeginn, "
    "nicht E-Mail-Datum). Bevorzuge: «Ort und Datum», «Erstellt am», Unterschrift/Fusszeile, "
    "«Stadt, den DD.MM.YYYY». Format JJJJ-MM-TT oder null."
)
