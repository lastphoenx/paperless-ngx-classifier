"""
Brillenpass-Extraktion — deterministische Parser (Fielmann zuerst) + Merge mit Vision.
"""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from typing import Any


def _norm_val(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", ".")
    s = re.sub(r"\s+", "", s)
    if not s:
        return None
    if s[0] not in "+-" and re.match(r"^\d", s):
        s = "+" + s if not s.startswith("-") else s
    return s


def _parse_eye_values(m: re.Match) -> dict:
    sph = _norm_val(m.group(3))
    cyl = _norm_val(m.group(4))
    achse = (m.group(5) or "").strip() or None
    tail = _parse_eye_tail(m.group(6) if m.lastindex and m.lastindex >= 6 else "")
    return {
        "sph": sph,
        "cyl": cyl,
        "achse": achse,
        **tail,
    }


def _parse_eye_tail(tail: str) -> dict:
    """Fielmann-Zeile nach Achse: Add oft vor Rechnungsbetrag (A 307.00), Prisma leer."""
    tail = (tail or "").strip()
    prisma = basis = add_v = None
    if not tail:
        return {"prisma": None, "basis": None, "add": None}

    # Rechnungsbetrag am Zeilenende (z. B. «A 307.00») — nicht Add/Prisma
    tail = re.sub(r"\s+(?:[A-Za-z]\s+)?\d{2,}[.,]\d{2}\s*$", "", tail).strip()
    tokens = [t for t in tail.split() if not (len(t) == 1 and t.isalpha())]

    for t in tokens:
        nv = _norm_val(t)
        if not nv:
            continue
        try:
            f = abs(float(nv.lstrip("+").replace(",", ".")))
        except ValueError:
            continue
        if f > 10:
            continue
        if 0.5 <= f <= 4.5:
            if add_v is None:
                add_v = nv
            elif prisma is None:
                prisma = nv
        elif prisma is None and f <= 12:
            prisma = nv

    return {"prisma": prisma, "basis": basis, "add": add_v}


def _sanitize_eye(eye: dict | None) -> dict | None:
    """Offensichtliche OCR-Fehler (Betrag als Add) entfernen."""
    if not eye:
        return eye
    out = dict(eye)
    for field, max_v in (("add", 5.0), ("prisma", 12.0)):
        v = out.get(field)
        if not v:
            continue
        try:
            f = abs(float(str(v).replace(",", ".").lstrip("+")))
            if f > max_v:
                out[field] = None
        except ValueError:
            out[field] = None
    return out


_EYE_LINE_RE = re.compile(
    r"(Fern|Nähe|Naehe)\s+(Rechts|Links)\s*:\s*"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"(\d+)\s+"
    r"(.*)$",
    re.IGNORECASE,
)

_OPTIKER_KEYWORDS = re.compile(
    r"Brillenglas|Sph\s+Cyl|Nähe\s+Rechts|Naehe\s+Rechts|Asph\.?\s*Hochbr|Kst\.\s*[\d.,]+",
    re.IGNORECASE,
)


def looks_like_optiker_rechnung(ocr_text: str, dokumenttyp_visuell: str = "") -> bool:
    if dokumenttyp_visuell and "rechnung" in dokumenttyp_visuell.lower():
        if _OPTIKER_KEYWORDS.search(ocr_text or ""):
            return True
    return bool(_OPTIKER_KEYWORDS.search(ocr_text or ""))


def parse_ch_date_short(text: str) -> str | None:
    """DD.MM.YY oder DD.MM.YYYY → YYYY-MM-DD."""
    for pat in [
        r"den\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})",
        r"vom\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y = 2000 + y if y < 70 else 1900 + y
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_fielmann_brillenpass(ocr_text: str) -> dict:
    """Deterministischer Fielmann-Parser aus OCR-Text."""
    text = ocr_text or ""
    fern: dict[str, dict | None] = {"rechts": None, "links": None}
    naehe: dict[str, dict | None] = {"rechts": None, "links": None}

    for m in _EYE_LINE_RE.finditer(text):
        dist = m.group(1).lower()
        side = m.group(2).lower()
        eye = _sanitize_eye(_parse_eye_values(m))
        bucket = fern if dist.startswith("fern") else naehe
        bucket["rechts" if side.startswith("recht") else "links"] = eye

    glas_desc = ""
    gm = re.search(
        r"Glas:\s*(.+?)(?:\n|Sph\s|Nähe|Naehe|Refraktion|Montage|Gesamtbetrag|$)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if gm:
        glas_desc = re.sub(r"\s+", " ", gm.group(1).strip())[:500]

    index = None
    im = re.search(r"Kst\.?\s*([\d.,]+)", text, re.IGNORECASE)
    if im:
        index = im.group(1).replace(",", ".")

    durchmesser = None
    dm = re.search(r"Durchmesser\s*(\d+)", text, re.IGNORECASE)
    if dm:
        durchmesser = int(dm.group(1))

    beschichtungen: list[str] = []
    for kw, label in [
        (r"Blaufil", "Blaufilter"),
        (r"Superentspiegelung", "Superentspiegelung"),
        (r"Entspiegelung", "Entspiegelung"),
        (r"Hart\s*Clean", "Hart Clean"),
    ]:
        if re.search(kw, text, re.IGNORECASE):
            beschichtungen.append(label)

    auftrag = ""
    am = re.search(r"Auftrag\s+([\d\s]+?)(?:\s+vom|\s*$)", text, re.IGNORECASE)
    if am:
        auftrag = re.sub(r"\s+", " ", am.group(1).strip())

    rechnung = ""
    rm = re.search(r"Rechnung:\s*([\d\s]+)", text, re.IGNORECASE)
    if rm:
        rechnung = re.sub(r"\s+", "", rm.group(1).strip())

    gueltig_ab = parse_ch_date_short(text)

    return {
        "parser": "fielmann",
        "gueltig_ab": gueltig_ab,
        "auftrag": auftrag,
        "rechnung": rechnung,
        "fern": fern,
        "naehe": naehe,
        "glas": {
            "beschreibung": glas_desc,
            "index": index,
            "durchmesser": durchmesser,
            "beschichtungen": beschichtungen,
        },
        "extraktion": {"quelle": "fielmann_regex", "confidence": "mittel"},
    }


def _empty_eye_block() -> dict:
    return {"rechts": None, "links": None}


def merge_brillenpass(parser_data: dict | None, vision_data: dict | None) -> dict:
    """Regex gewinnt bei gesetzten Augenwerten; Vision füllt Lücken."""
    base = deepcopy(parser_data) if parser_data else {
        "parser": "vision",
        "fern": _empty_eye_block(),
        "naehe": _empty_eye_block(),
        "glas": {"beschreibung": "", "index": None, "durchmesser": None, "beschichtungen": []},
        "extraktion": {"quelle": "vision", "confidence": "tief"},
    }
    if not vision_data:
        return base

    for dist in ("fern", "naehe"):
        for side in ("rechts", "links"):
            v_eye = (vision_data.get(dist) or {}).get(side)
            p_eye = (base.get(dist) or {}).get(side)
            if v_eye and not p_eye:
                base.setdefault(dist, _empty_eye_block())[side] = _sanitize_eye(v_eye)
            elif v_eye and p_eye:
                merged = dict(p_eye)
                for k, val in v_eye.items():
                    if not merged.get(k) and val:
                        merged[k] = val
                base[dist][side] = _sanitize_eye(merged)
            elif p_eye:
                base[dist][side] = _sanitize_eye(p_eye)
            elif v_eye:
                base[dist][side] = _sanitize_eye(v_eye)

    v_glas = vision_data.get("glas") or {}
    b_glas = base.setdefault("glas", {})
    for k in ("beschreibung", "index", "durchmesser", "beschichtungen"):
        if not b_glas.get(k) and v_glas.get(k):
            b_glas[k] = v_glas[k]

    for k in ("auftrag", "rechnung", "gueltig_ab"):
        if not base.get(k) and vision_data.get(k):
            base[k] = vision_data[k]

    sources = [base.get("extraktion", {}).get("quelle", "")]
    if vision_data:
        sources.append("vision")
    base["extraktion"] = {
        "quelle": "+".join(s for s in sources if s) or "merged",
        "confidence": base.get("extraktion", {}).get("confidence") or "mittel",
    }
    return base


def has_brillenpass_values(data: dict) -> bool:
    """Mindestens ein Auge mit sph oder Glas-Index."""
    for dist in ("fern", "naehe"):
        block = data.get(dist) or {}
        for side in ("rechts", "links"):
            eye = block.get(side)
            if eye and eye.get("sph"):
                return True
    glas = data.get("glas") or {}
    return bool(glas.get("index") or glas.get("beschreibung"))


def corr_supports_brillenpass(corr_entry: dict | None) -> tuple[bool, str]:
    """(aktiv, parser_name) aus correspondents.json brillenpass-Block."""
    if not corr_entry:
        return False, ""
    bp = corr_entry.get("brillenpass") or {}
    if bp.get("aktiv"):
        return True, (bp.get("parser") or "fielmann").lower()
    return False, ""


def build_version_id(gueltig_ab: str, korrespondent: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", korrespondent.lower()).strip("-")[:20]
    return f"bp-{gueltig_ab}-{slug}"


def compute_brillenpass_diff(old: dict | None, new: dict) -> dict:
    """Flache Diff-Map: pfad → {alt, neu}."""
    if not old:
        return {}

    def _flat(prefix: str, obj: Any, out: dict) -> None:
        if obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                _flat(f"{prefix}.{k}" if prefix else k, v, out)
        else:
            out[prefix] = obj

    old_flat: dict = {}
    new_flat: dict = {}
    for dist in ("fern", "naehe"):
        for side in ("rechts", "links"):
            _flat(f"{dist}.{side}", (old.get(dist) or {}).get(side), old_flat)
            _flat(f"{dist}.{side}", (new.get(dist) or {}).get(side), new_flat)
    _flat("glas", old.get("glas"), old_flat)
    _flat("glas", new.get("glas"), new_flat)

    diff = {}
    for key in set(old_flat) | set(new_flat):
        o, n = old_flat.get(key), new_flat.get(key)
        if o != n:
            diff[key] = {"alt": o, "neu": n}
    return diff
