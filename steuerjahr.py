"""
Steuerjahr-Inferenz — welches Steuerjahr ein Beleg betrifft.

Steuerjahr ≠ Ausstellungsdatum:
  - 3a-Bescheinigung März 2026 → Steuerjahr 2025
  - Energierechnung Aug 2025 → Steuerjahr 2025
  - Lohnausweis 2025 → Steuerjahr 2025
"""
from __future__ import annotations

import re
from datetime import date

# Dokumenttypen mit typischem «Jahr im Titel» oder Jahresbezug
_JAHRES_BELEGE = frozenset({
    "lohnausweis", "jahreslohnausweis", "lohnabrechnung",
    "steuerwertbescheinigung", "steuerbescheinigung", "steuerdokument",
    "kontoauszug", "saldo", "jahresabschluss", "jahresrechnung",
    "prämienbescheinigung", "spendenbescheinigung", "zinsbescheinigung",
    "hypothekarzins", "säule 3a", "saeule 3a", "3a",
})

# Rechnungen / Aufwendungen: Ausstellungsjahr = Steuerjahr
_AUFWAND_BELEGE = frozenset({
    "rechnung", "servicerechnung", "faktura", "quittung",
})

_YEAR_RE = re.compile(r"\b(20[12]\d)\b")
_SALDO_PER_RE = re.compile(
    r"(?:saldo|stand|per|stichtag|bescheinigt|gültig)\s*(?:per|am|vom)?\s*"
    r"(?:31\.?\s*12\.?\s*|den\s+)?(20[12]\d)",
    re.IGNORECASE,
)
_PERIODE_RE = re.compile(
    r"(?:steuerperiode|abrechnungsjahr|beitragsjahr|versicherungsjahr|jahr)\s*[:\s]?\s*(20[12]\d)",
    re.IGNORECASE,
)
_LOHNAUSWEIS_RE = re.compile(
    r"(?:lohnausweis|jahreslohnausweis|lohn\s*abrechnung)\s*[:\s]?\s*(20[12]\d)",
    re.IGNORECASE,
)


def _parse_iso_year(iso: str | None) -> int | None:
    if not iso or len(str(iso)) < 4:
        return None
    try:
        y = int(str(iso)[:4])
        return y if 2000 <= y <= 2099 else None
    except ValueError:
        return None


def _years_in_context(text: str, window: int = 80) -> list[int]:
    """Jahreszahlen mit Kontext-Priorität (Saldo, Periode, Lohnausweis)."""
    if not text:
        return []
    found: list[int] = []

    def _add(y: int) -> None:
        if 2000 <= y <= 2099 and y not in found:
            found.append(y)

    for m in _SALDO_PER_RE.finditer(text):
        _add(int(m.group(1)))
    for m in _PERIODE_RE.finditer(text):
        _add(int(m.group(1)))
    for m in _LOHNAUSWEIS_RE.finditer(text):
        _add(int(m.group(1)))

    low = text.lower()
    for m in _YEAR_RE.finditer(text):
        y = int(m.group(1))
        start = max(0, m.start() - window)
        ctx = low[start : m.end() + 20]
        if any(k in ctx for k in (
            "steuer", "lohn", "saldo", "beschein", "periode", "abrechnung",
            "3a", "säule", "hypothek", "zins", "spende", "prämie", "versicherung",
            "konto", "depot", "wert", "beitrag",
        )):
            _add(y)
    return found


def _doctype_bucket(doctyp_name: str) -> str:
    n = (doctyp_name or "").lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    if any(k in n for k in _AUFWAND_BELEGE):
        return "aufwand"
    if any(k in n for k in _JAHRES_BELEGE):
        return "jahr"
    return "neutral"


def _text_suggests_jahresbeleg(text: str) -> bool:
    low = (text or "").lower().replace("ä", "a").replace("ö", "o").replace("ü", "u")
    return any(k in low for k in (
        "3a", "saeule", "sauele", "lohnausweis", "steuerwert", "hypothek",
        "zinsbeschein", "spendenbeschein", "jahresabschluss", "saldo per",
    ))


def infer_steuerjahr(
    *,
    ocr_text: str = "",
    vision_meta: dict | None = None,
    ausstellungsdatum: str | None = None,
    doctyp_name: str = "",
    title: str = "",
) -> int | None:
  """
  Steuerjahr schätzen. None wenn nicht ermittelbar.
  """
    vision_meta = vision_meta or {}
    combined = "\n".join(filter(None, [
        title,
        vision_meta.get("titel", ""),
        vision_meta.get("betreff", ""),
        ocr_text or "",
    ]))
    issue_year = _parse_iso_year(ausstellungsdatum) or _parse_iso_year(vision_meta.get("datum"))
    bucket = _doctype_bucket(doctyp_name)
    if bucket == "neutral" and _text_suggests_jahresbeleg(combined):
        bucket = "jahr"

    years = _years_in_context(combined)
    if len(years) == 1:
        return years[0]
    if len(years) > 1:
        # Mehrdeutig — bei Jahresbelegen oft das kleinere (ältere) Steuerjahr
        if bucket == "jahr":
            return min(years)
        if issue_year and issue_year in years:
            return issue_year
        return None

    if issue_year:
        if bucket == "aufwand":
            return issue_year
        # Jahresbeleg eingegangen Jan–März: oft Vorjahr
        try:
            parts = str(ausstellungsdatum or vision_meta.get("datum") or "")[:10].split("-")
            if len(parts) == 3:
                y, mo = int(parts[0]), int(parts[1])
                if bucket == "jahr" and mo <= 3:
                    return y - 1
        except (ValueError, IndexError):
            pass
        if bucket == "jahr":
            return issue_year - 1 if issue_year else None
        return issue_year

    return None
