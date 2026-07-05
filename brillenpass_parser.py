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
    """Offensichtliche OCR/Vision-Fehler (Betrag als Add, Add in Prisma) bereinigen."""
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
    # Vision verwechselt oft Add (1.75) mit Prisma — Add-Wert in Prisma-Spalte
    prisma, add = out.get("prisma"), out.get("add")
    if prisma and not add:
        try:
            f = abs(float(str(prisma).replace(",", ".").lstrip("+")))
            if 0.5 <= f <= 4.5:
                out["add"] = prisma if str(prisma).startswith(("+", "-")) else f"+{prisma}"
                out["prisma"] = None
                out["basis"] = None
        except ValueError:
            pass
    return out


def _vals_close(a, b) -> bool:
    if not a or not b:
        return False
    try:
        return abs(float(str(a).replace(",", ".").lstrip("+")) - float(str(b).replace(",", ".").lstrip("+"))) < 0.01
    except ValueError:
        return str(a).strip() == str(b).strip()


def _merge_eye(p_eye: dict | None, v_eye: dict | None) -> dict | None:
    if p_eye and not v_eye:
        return _sanitize_eye(p_eye)
    if v_eye and not p_eye:
        return _sanitize_eye(v_eye)
    if not p_eye and not v_eye:
        return None
    merged = dict(p_eye)
    for k, val in (v_eye or {}).items():
        if not val or merged.get(k):
            continue
        if k == "prisma" and merged.get("add") and _vals_close(val, merged["add"]):
            continue
        if k == "add" and merged.get("prisma") and _vals_close(val, merged["prisma"]):
            continue
        merged[k] = val
    return _sanitize_eye(merged)


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
            merged_eye = _merge_eye(p_eye, v_eye)
            if merged_eye:
                base.setdefault(dist, _empty_eye_block())[side] = merged_eye

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


PARSER_LABELS: dict[str, str] = {
    "fielmann": "Fielmann Rechnung",
    "fielmann_pass": "Fielmann Brillenpass (Karte)",
    "mcoptic_pass": "McOptic Brillenpass",
    "augenarzt": "Augenarzt-Verordnung",
    "optik_meyer_moehlin": "Optik Meyer Möhlin",
}

PARSER_NAMES = list(PARSER_LABELS.keys())


def corr_supports_brillenpass(corr_entry: dict | None) -> tuple[bool, str]:
    """(aktiv, parser_name) aus correspondents.json brillenpass-Block."""
    if not corr_entry:
        return False, ""
    bp = corr_entry.get("brillenpass") or {}
    if bp.get("aktiv"):
        parser = (bp.get("parser") or "fielmann").lower()
        if parser not in PARSER_NAMES:
            parser = "fielmann"
        return True, parser
    return False, ""


def build_version_id(gueltig_ab: str, korrespondent: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", korrespondent.lower()).strip("-")[:20]
    return f"bp-{gueltig_ab}-{slug}"


def _bp_base(parser: str, **kwargs) -> dict:
    return {
        "parser": parser,
        "gueltig_ab": kwargs.get("gueltig_ab"),
        "auftrag": kwargs.get("auftrag", ""),
        "rechnung": kwargs.get("rechnung", ""),
        "fern": kwargs.get("fern") or _empty_eye_block(),
        "naehe": kwargs.get("naehe") or _empty_eye_block(),
        "glas": kwargs.get("glas") or {
            "beschreibung": "", "index": None, "durchmesser": None, "beschichtungen": [],
        },
        "extraktion": {"quelle": f"{parser}_regex", "confidence": "mittel"},
    }


def _side_key(label: str) -> str:
    return "rechts" if label.lower().startswith(("r", "recht")) else "links"


def _eye_from_parts(sph, cyl, achse, add=None) -> dict | None:
    if not sph:
        return None
    return _sanitize_eye({
        "sph": _norm_val(sph),
        "cyl": _norm_val(cyl),
        "achse": re.sub(r"\D", "", str(achse)) if achse else None,
        "prisma": None,
        "basis": None,
        "add": _norm_val(add),
    })


def _parse_pass_date(text: str) -> str | None:
    for pat in [
        r"Datum:\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})",
        r"Verordnung\s+vom\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})",
        r"ausgestellt\s+(?:am\s+)?(\d{1,2})\.(\d{1,2})\.(\d{2,4})",
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
    return parse_ch_date_short(text)


_FIELMANN_PASS_RL = re.compile(
    r"(?:^|\n)\s*(R|L|Rechts|Links)\s*[:.]?\s*"
    r"(?:S\s*)?"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"(?:C\s*)?"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"(?:A\s*)?"
    r"(\d+)\s*°?"
    r"(?:\s+ADD\s+([+\-]?\s*[\d.,]+))?",
    re.IGNORECASE,
)

_FIELMANN_PASS_SIMPLE = re.compile(
    r"(?:^|\n)\s*(R|L|Rechts|Links)\s*[:.]?\s*"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"(\d+)\s+"
    r"([+\-]?\s*[\d.,]+)\s*(?:\n|$)",
    re.IGNORECASE,
)

_MCOPTIC_RL = re.compile(
    r"(?:^|\n)\s*(R|L|Rechts|Links)\s*[:.]?\s*"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"(\d+)\s+"
    r"([+\-]?\s*[\d.,]+)"
    r"(?:\s+[\d.,]+)?",
    re.IGNORECASE,
)

_AUGENARZT_RL = re.compile(
    r"(?:^|\n)\s*(Rechts|Links|R|L)\s*[:.]?\s*"
    r"(?:sph\.?\s*|s\s*)?"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"(?:cyl\.?|zyl\.?|c\s*)"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"(?:axis|achse|a)\s*"
    r"(\d+)\s*°?"
    r"(?:\s+(?:add\.?|addition)\s*([+\-]?\s*[\d.,]+))?",
    re.IGNORECASE,
)


def _fill_naehe_from_matches(text: str, pattern: re.Pattern) -> dict[str, dict | None]:
    naehe: dict[str, dict | None] = {"rechts": None, "links": None}
    for m in pattern.finditer(text):
        side = _side_key(m.group(1))
        add = m.group(5) if m.lastindex and m.lastindex >= 5 else None
        eye = _eye_from_parts(m.group(2), m.group(3), m.group(4), add)
        if eye:
            naehe[side] = eye
    return naehe


def parse_fielmann_pass(ocr_text: str) -> dict:
    """Physische Fielmann-Brillenpass-Karte (nicht Rechnung)."""
    text = ocr_text or ""
    naehe = _fill_naehe_from_matches(text, _FIELMANN_PASS_RL)
    if not naehe["rechts"] and not naehe["links"]:
        naehe = _fill_naehe_from_matches(text, _FIELMANN_PASS_SIMPLE)

    glas_desc = ""
    gm = re.search(r"Glas:\s*(.+?)(?:\n|ADD|Datum|$)", text, re.IGNORECASE | re.DOTALL)
    if gm:
        glas_desc = re.sub(r"\s+", " ", gm.group(1).strip())[:500]

    index = None
    im = re.search(r"Kst\.?\s*([\d.,]+)|Index\s*([\d.,]+)", text, re.IGNORECASE)
    if im:
        index = (im.group(1) or im.group(2) or "").replace(",", ".")

    return _bp_base(
        "fielmann_pass",
        gueltig_ab=_parse_pass_date(text),
        naehe=naehe,
        glas={"beschreibung": glas_desc, "index": index, "durchmesser": None, "beschichtungen": []},
    )


def parse_mcoptic_pass(ocr_text: str) -> dict:
    """McOptic Brillenpass-Karte (SPH ZYL ACHSE ADD PD)."""
    text = ocr_text or ""
    naehe = _fill_naehe_from_matches(text, _MCOPTIC_RL)

    glas_desc = ""
    for pat in [
        r"(?:Glas|Lens|Brille)\s*[:.]?\s*(.+?)(?:\n|R\s*:|Rechts)",
        r"(Inside|Desk|Progressive|Office)\s+[\w\s]+",
    ]:
        gm = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if gm:
            glas_desc = re.sub(r"\s+", " ", (gm.group(1) if gm.lastindex else gm.group(0)).strip())[:500]
            break

    return _bp_base(
        "mcoptic_pass",
        gueltig_ab=_parse_pass_date(text),
        naehe=naehe,
        glas={"beschreibung": glas_desc, "index": None, "durchmesser": None, "beschichtungen": []},
    )


def parse_augenarzt(ocr_text: str) -> dict:
    """Augenarzt-Verordnung (Rechts/Links mit Sph, Cyl, Achse, Add)."""
    text = ocr_text or ""
    naehe = _fill_naehe_from_matches(text, _AUGENARZT_RL)

    # Fern/Nähe-Tabelle: «Fern Rechts» / «Nähe Rechts» wie Fielmann-Rechnung
    fern: dict[str, dict | None] = {"rechts": None, "links": None}
    for m in _EYE_LINE_RE.finditer(text):
        dist = m.group(1).lower()
        side = "rechts" if m.group(2).lower().startswith("recht") else "links"
        eye = _sanitize_eye(_parse_eye_values(m))
        if dist.startswith("fern"):
            fern[side] = eye
        else:
            naehe[side] = eye

    if not naehe["rechts"] and not naehe["links"]:
        naehe = _fill_naehe_from_matches(text, _FIELMANN_PASS_SIMPLE)
    if not naehe["rechts"] and not naehe["links"]:
        naehe = _fill_naehe_from_matches(text, _MCOPTIC_RL)

    return _bp_base(
        "augenarzt",
        gueltig_ab=_parse_pass_date(text),
        fern=fern,
        naehe=naehe,
    )


def parse_optik_meyer_moehlin(ocr_text: str) -> dict:
    """Optik Meyer Möhlin — Verordnung (Seite 2) oder Werte unten links auf Rechnung."""
    text = ocr_text or ""
    # Unteres Viertel bevorzugen (Werte «unten links» auf Rechnung)
    tail = text[max(0, len(text) * 3 // 4):] if len(text) > 400 else text
    base = parse_augenarzt(tail if _AUGENARZT_RL.search(tail) else text)
    base["parser"] = "optik_meyer_moehlin"
    base["extraktion"]["quelle"] = "optik_meyer_moehlin_regex"

    if not base.get("gueltig_ab"):
        base["gueltig_ab"] = _parse_pass_date(text)

    glas_desc = ""
    gm = re.search(
        r"Glas(?:art)?\s*[:.]?\s*(.+?)(?:\n|Rechts|Links|R\s*:|Total|$)",
        text, re.IGNORECASE | re.DOTALL,
    )
    if gm:
        glas_desc = re.sub(r"\s+", " ", gm.group(1).strip())[:500]
    if glas_desc:
        base["glas"]["beschreibung"] = glas_desc

    rechnung = ""
    rm = re.search(r"Rechnung\s*(?:Nr\.?)?\s*[:.]?\s*([\d\s/\-]+)", text, re.IGNORECASE)
    if rm:
        rechnung = re.sub(r"\s+", "", rm.group(1).strip())
    base["rechnung"] = rechnung
    return base


_PARSERS: dict[str, Any] = {
    "fielmann": parse_fielmann_brillenpass,
    "fielmann_pass": parse_fielmann_pass,
    "mcoptic_pass": parse_mcoptic_pass,
    "augenarzt": parse_augenarzt,
    "optik_meyer_moehlin": parse_optik_meyer_moehlin,
}


def parse_by_parser(parser_name: str, ocr_text: str) -> dict | None:
    fn = _PARSERS.get((parser_name or "").lower())
    if not fn:
        return None
    return fn(ocr_text or "")


def _detect_fielmann(text: str) -> int:
    score = 0
    if re.search(r"Nähe\s+Rechts|Naehe\s+Rechts", text, re.I):
        score += 3
    if re.search(r"Brillenglas|Asph\.?\s*Hochbr|Gesamtbetrag", text, re.I):
        score += 2
    if re.search(r"Fielmann", text, re.I):
        score += 1
    return score


def _detect_fielmann_pass(text: str) -> int:
    score = 0
    if re.search(r"Brillenpass|ADD\s+\d", text, re.I):
        score += 2
    if _FIELMANN_PASS_RL.search(text) or _FIELMANN_PASS_SIMPLE.search(text):
        score += 3
    if re.search(r"Fielmann|Zeiss", text, re.I):
        score += 1
    if re.search(r"Nähe\s+Rechts|Gesamtbetrag", text, re.I):
        score -= 2
    return max(0, score)


def _detect_mcoptic_pass(text: str) -> int:
    score = 0
    if re.search(r"Mc\s*Optic|McOptic", text, re.I):
        score += 3
    if re.search(r"SPH\s+ZYL|ZYL\s+ACHSE", text, re.I):
        score += 2
    if _MCOPTIC_RL.search(text):
        score += 3
    return score


def _detect_augenarzt(text: str) -> int:
    score = 0
    if re.search(r"Verordnung|Augenarzt|Augenärzt|Dioptrie|Refraktion", text, re.I):
        score += 2
    if _AUGENARZT_RL.search(text):
        score += 3
    if re.search(r"Fielmann|Mc\s*Optic|Optik\s+Meyer", text, re.I):
        score -= 1
    return max(0, score)


def _detect_optik_meyer(text: str) -> int:
    score = 0
    if re.search(r"Optik\s+Meyer", text, re.I):
        score += 3
    if re.search(r"Möhlin|Moehlin", text, re.I):
        score += 2
    if (
        _AUGENARZT_RL.search(text)
        or _FIELMANN_PASS_SIMPLE.search(text)
        or _MCOPTIC_RL.search(text)
    ):
        score += 2
    return score


_DETECTORS: dict[str, Any] = {
    "fielmann": _detect_fielmann,
    "fielmann_pass": _detect_fielmann_pass,
    "mcoptic_pass": _detect_mcoptic_pass,
    "augenarzt": _detect_augenarzt,
    "optik_meyer_moehlin": _detect_optik_meyer,
}


def detect_parser(ocr_text: str) -> str | None:
    """Besten Parser anhand OCR-Heuristik wählen."""
    text = ocr_text or ""
    if not text.strip():
        return None
    scores = {name: fn(text) for name, fn in _DETECTORS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def looks_like_brillenpass_document(
    ocr_text: str, parser_name: str, dokumenttyp_visuell: str = "",
) -> bool:
    """Parser-spezifische Erkennung (Pass, Verordnung, Rechnung)."""
    name = (parser_name or "").lower()
    if name in _DETECTORS:
        return _DETECTORS[name](ocr_text or "") > 0
    return looks_like_optiker_rechnung(ocr_text, dokumenttyp_visuell)


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
