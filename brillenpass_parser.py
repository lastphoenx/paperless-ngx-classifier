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
    # Vision setzt Augen-Label (R/L) oder Achsen-Symbol (A°) fälschlich als Prisma-Basis
    basis = (out.get("basis") or "").strip()
    if basis.upper() in ("R", "L") or basis.replace("°", "").upper() in ("A", "A°"):
        out["basis"] = None
    # Fehlendes Minus bei Sph (häufig Vision): Cyl negativ, Sph positiv → Kurzsichtigkeit
    sph, cyl = out.get("sph"), out.get("cyl")
    if sph and cyl:
        try:
            sf = float(str(sph).replace(",", ".").lstrip("+"))
            cf = float(str(cyl).replace(",", ".").lstrip("+"))
            if sf > 0 and cf < -0.01 and sf >= 0.25:
                out["sph"] = f"-{sf:g}"
        except ValueError:
            pass
    # Add 0.00 bei Ferngläsern → leer
    if out.get("add") is not None:
        try:
            if abs(float(str(out["add"]).replace(",", ".").lstrip("+"))) < 0.25:
                out["add"] = None
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
        "parser": "fielmann_rechnung",
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
        "extraktion": {"quelle": "fielmann_rechnung_regex", "confidence": "mittel"},
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

    base = _reconcile_split_eyes(base, parser_data)

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


# Parser-IDs: {optiker}_{dokumentformat} — Auto-Erkennung wählt Rechnung vs. Brillenpass vs. Verordnung
PARSER_LABELS: dict[str, str] = {
    "fielmann_rechnung": "Fielmann · Rechnung (A4)",
    "fielmann_brillenpass": "Fielmann · Brillenpass (Karte)",
    "mcoptic_rechnung": "McOptic · Rechnung/Quittung (A4)",
    "mcoptic_brillenpass": "McOptic · Brillenpass (Karte)",
    "augenarzt_verordnung": "Augenarzt · Verordnung",
    "optik_meyer_rechnung": "Optik Meyer · Rechnung/Verordnung",
}

PARSER_FORMAT: dict[str, str] = {
    "fielmann_rechnung": "rechnung",
    "fielmann_brillenpass": "brillenpass",
    "mcoptic_rechnung": "rechnung",
    "mcoptic_brillenpass": "brillenpass",
    "augenarzt_verordnung": "verordnung",
    "optik_meyer_rechnung": "rechnung",
}

PARSER_VENDOR: dict[str, str] = {
    "fielmann_rechnung": "fielmann",
    "fielmann_brillenpass": "fielmann",
    "mcoptic_rechnung": "mcoptic",
    "mcoptic_brillenpass": "mcoptic",
    "augenarzt_verordnung": "augenarzt",
    "optik_meyer_rechnung": "optik_meyer",
}

VENDOR_LABELS: dict[str, str] = {
    "fielmann": "Fielmann",
    "mcoptic": "McOptic",
    "optik_meyer": "Optik Meyer Möhlin",
    "augenarzt": "Augenarzt (Verordnung)",
}

VENDOR_PARSERS: dict[str, list[str]] = {
    "fielmann": ["fielmann_rechnung", "fielmann_brillenpass"],
    "mcoptic": ["mcoptic_rechnung", "mcoptic_brillenpass"],
    "optik_meyer": ["optik_meyer_rechnung"],
    "augenarzt": ["augenarzt_verordnung"],
}

# Abwärtskompatibilität alter Parser-IDs in correspondents.json
PARSER_ALIASES: dict[str, str] = {
    "fielmann": "fielmann_rechnung",
    "fielmann_pass": "fielmann_brillenpass",
    "mcoptic_pass": "mcoptic_brillenpass",
    "augenarzt": "augenarzt_verordnung",
    "optik_meyer_moehlin": "optik_meyer_rechnung",
}

PARSER_NAMES = list(PARSER_LABELS.keys())


def normalize_parser_name(name: str) -> str:
    n = str(name or "").strip().lower()
    return PARSER_ALIASES.get(n, n)


def vendor_from_parser(parser_name: str) -> str | None:
    return PARSER_VENDOR.get(normalize_parser_name(parser_name))


def corr_brillenpass_parsers(corr_entry: dict | None) -> list[str]:
    """Erlaubte Parser-Kandidaten für Auto-Erkennung (Vendor oder explizite Liste)."""
    if not corr_entry:
        return []
    bp = corr_entry.get("brillenpass") or {}
    if not bp.get("aktiv"):
        return []

    vendor = str(bp.get("vendor") or "").strip().lower()
    if vendor and vendor in VENDOR_PARSERS:
        return list(VENDOR_PARSERS[vendor])

    raw = bp.get("parsers") or []
    if isinstance(raw, str):
        raw = [raw]
    if not raw and bp.get("parser"):
        raw = [bp.get("parser")]

    explicit: list[str] = []
    vendors: set[str] = set()
    for p in raw:
        name = normalize_parser_name(str(p or "").strip())
        if name in PARSER_NAMES and name not in explicit:
            explicit.append(name)
            v = vendor_from_parser(name)
            if v:
                vendors.add(v)

    if not explicit:
        return []

    # Ein Optiker → alle Formate dieses Vendors (Rechnung + Brillenpass auto)
    if len(vendors) == 1:
        return list(VENDOR_PARSERS[vendors.pop()])

    return explicit


def corr_supports_brillenpass(corr_entry: dict | None) -> tuple[bool, str]:
    """(aktiv, erster_parser) — Abwärtskompatibilität."""
    parsers = corr_brillenpass_parsers(corr_entry)
    if parsers:
        return True, parsers[0]
    return False, ""


def looks_like_brillenpass_any(
    ocr_text: str, parser_names: list[str], dokumenttyp_visuell: str = "",
    vision_meta: dict | None = None,
) -> bool:
    allowed = [normalize_parser_name(p) for p in parser_names]
    if detect_parser(
        ocr_text,
        allowed=allowed,
        dokumenttyp_visuell=dokumenttyp_visuell,
        vision_meta=vision_meta,
    ):
        return True
    return any(
        looks_like_brillenpass_document(ocr_text, p, dokumenttyp_visuell)
        for p in allowed
    )


def _vision_format_boost(dokumenttyp_visuell: str, fmt: str) -> int:
    """Vision-Freitext (dokumenttyp_visuell) bevorzugt passendes Dokumentformat."""
    vis = (dokumenttyp_visuell or "").lower()
    if not vis or not fmt:
        return 0
    if fmt == "brillenpass":
        if any(k in vis for k in ("brillenpass", "pass", "karte", "kartenformat", "plastikkarte")):
            return 5
        if any(k in vis for k in ("rechnung", "quittung", "a4", "krankenkassenexemplar")):
            return -4
    elif fmt == "rechnung":
        if any(k in vis for k in ("rechnung", "quittung", "krankenkassenexemplar", "invoice", "a4")):
            return 5
        if any(k in vis for k in ("brillenpass", "karte", "pass", "plastik")):
            return -4
    elif fmt == "verordnung":
        if any(k in vis for k in ("verordnung", "rezept", "augentest", "ärztlich")):
            return 5
    return 0


def _vision_layout_boost(vision_meta: dict | None, fmt: str) -> int:
    """Layout/Besonderheiten aus Vision (Karte vs. A4)."""
    if not vision_meta or not fmt:
        return 0
    blob = " ".join(
        str(vision_meta.get(k) or "")
        for k in ("layout", "besonderheiten", "dokumenttyp_visuell")
    ).lower()
    if not blob:
        return 0
    if fmt == "brillenpass":
        if any(k in blob for k in ("karte", "brillenpass", "kleinformat", "hochformat", "plastik", "wallet")):
            return 3
        if any(k in blob for k in ("a4", "querformat", "rechnung", "quittung")):
            return -2
    elif fmt == "rechnung":
        if any(k in blob for k in ("a4", "rechnung", "quittung", "querformat", "mehrseitig")):
            return 3
        if any(k in blob for k in ("karte", "brillenpass", "kleinformat")):
            return -2
    return 0


def detect_parser(
    ocr_text: str,
    *,
    allowed: list[str] | None = None,
    dokumenttyp_visuell: str = "",
    vision_meta: dict | None = None,
) -> str | None:
    """Besten Parser per OCR + Vision (Format: Rechnung vs. Brillenpass vs. Verordnung)."""
    text = ocr_text or ""
    if not text.strip():
        return None

    candidates = [normalize_parser_name(p) for p in (allowed or PARSER_NAMES)]
    candidates = [p for p in candidates if p in _DETECTORS]
    if not candidates:
        return None

    scores: dict[str, int] = {}
    for name in candidates:
        score = _DETECTORS[name](text)
        fmt = PARSER_FORMAT.get(name, "")
        score += _vision_format_boost(dokumenttyp_visuell, fmt)
        score += _vision_layout_boost(vision_meta, fmt)
        scores[name] = score

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def parse_brillenpass_auto(
    ocr_text: str,
    parser_names: list[str],
    *,
    dokumenttyp_visuell: str = "",
    vision_meta: dict | None = None,
) -> dict | None:
    """Einen passenden Parser wählen (Format-Erkennung) und parsen — kein Multi-Merge."""
    allowed = [normalize_parser_name(p) for p in parser_names]
    chosen = detect_parser(
        ocr_text,
        allowed=allowed,
        dokumenttyp_visuell=dokumenttyp_visuell,
        vision_meta=vision_meta,
    )
    if not chosen:
        return None
    data = parse_by_parser(chosen, ocr_text)
    if not data or not has_brillenpass_values(data):
        return None
    ext = data.setdefault("extraktion", {})
    ext["parser_detected"] = chosen
    ext["parser_format"] = PARSER_FORMAT.get(chosen, "")
    ext["parsers_allowed"] = [p for p in allowed if p in PARSER_NAMES]
    return data


def parse_brillenpass_with_parsers(
    ocr_text: str,
    parser_names: list[str],
    *,
    dokumenttyp_visuell: str = "",
    vision_meta: dict | None = None,
) -> dict | None:
    """Alias — wählt einen Parser per Auto-Erkennung (kein Merge mehr)."""
    return parse_brillenpass_auto(
        ocr_text,
        parser_names,
        dokumenttyp_visuell=dokumenttyp_visuell,
        vision_meta=vision_meta,
    )


def brillenpass_dates_close(d1: str | None, d2: str | None, max_days: int = 21) -> bool:
    """Gleiche Brillenpass-Periode (Rechnung + Pass wenige Tage auseinander)."""
    if not d1 or not d2:
        return False
    try:
        from datetime import date
        a = date.fromisoformat(str(d1)[:10])
        b = date.fromisoformat(str(d2)[:10])
        return abs((a - b).days) <= max_days
    except ValueError:
        return False


def find_brillenpass_period_duplicate(
    versionen: list[dict],
    gueltig_ab: str,
    korrespondent: str,
    *,
    max_days: int = 21,
) -> int | None:
    """Index einer Version gleicher Periode — zum Ersetzen statt Duplikat."""
    for i in range(len(versionen) - 1, -1, -1):
        v = versionen[i]
        if (v.get("korrespondent") or "").lower() != (korrespondent or "").lower():
            continue
        if brillenpass_dates_close(v.get("gueltig_ab"), gueltig_ab, max_days):
            return i
    return None


def merge_brillenpass_version(existing: dict, incoming: dict) -> dict:
    """Bestehende freigegebene Version mit neuem Vorschlag anreichern (Dedup)."""
    out = dict(existing)
    for key in ("auftrag", "rechnung", "gueltig_ab"):
        if not out.get(key) and incoming.get(key):
            out[key] = incoming[key]
    for dist in ("fern", "naehe"):
        out.setdefault(dist, {"rechts": None, "links": None})
        inc_dist = incoming.get(dist) or {}
        for side in ("rechts", "links"):
            if not out[dist].get(side) and inc_dist.get(side):
                out[dist][side] = inc_dist[side]
            elif out[dist].get(side) and inc_dist.get(side):
                out[dist][side] = _merge_eye(out[dist][side], inc_dist[side])
    g_out = out.setdefault("glas", {})
    g_inc = incoming.get("glas") or {}
    for k in ("beschreibung", "index", "durchmesser", "beschichtungen"):
        if not g_out.get(k) and g_inc.get(k):
            g_out[k] = g_inc[k]
    if incoming.get("document_id") and not out.get("document_id"):
        out["document_id"] = incoming["document_id"]
    ext = out.setdefault("extraktion", {})
    inc_ext = incoming.get("extraktion") or {}
    prev_src = ext.get("quelle", "")
    new_src = inc_ext.get("quelle", "")
    if new_src and new_src not in prev_src:
        ext["quelle"] = f"{prev_src}+{new_src}" if prev_src else new_src
    ext["dedup_merged"] = True
    return out


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


def _add_is_near(add_val: str | None) -> bool:
    if not add_val:
        return False
    try:
        return abs(float(str(add_val).replace(",", ".").lstrip("+"))) >= 0.25
    except ValueError:
        return False


def _reconcile_split_eyes(data: dict, parser_data: dict | None) -> dict:
    """
    Parser+Vision-Mix: z. B. R nur in fern, L nur in naehe → einen Block.
    McOptic-Pass mit Add≈0 gehört in fern.
    """
    fern = dict(data.get("fern") or _empty_eye_block())
    naehe = dict(data.get("naehe") or _empty_eye_block())
    f_sides = {s for s in ("rechts", "links") if (fern.get(s) or {}).get("sph")}
    n_sides = {s for s in ("rechts", "links") if (naehe.get(s) or {}).get("sph")}

    if f_sides and n_sides and not (f_sides & n_sides):
        target = "fern"
        if parser_data:
            pf = sum(
                1 for s in ("rechts", "links")
                if ((parser_data.get("fern") or {}).get(s) or {}).get("sph")
            )
            pn = sum(
                1 for s in ("rechts", "links")
                if ((parser_data.get("naehe") or {}).get(s) or {}).get("sph")
            )
            if pn > pf:
                target = "naehe"
        merged_block = _empty_eye_block()
        for side in ("rechts", "links"):
            if target == "fern":
                e1, e2 = fern.get(side), naehe.get(side)
            else:
                e1, e2 = naehe.get(side), fern.get(side)
            eye = _merge_eye(e1, e2)
            if eye:
                merged_block[side] = eye
        if target == "fern":
            data["fern"], data["naehe"] = merged_block, _empty_eye_block()
        else:
            data["fern"], data["naehe"] = _empty_eye_block(), merged_block
        return data

    # Parser hatte alles in einem Block, Vision verteilte — Parser-Bucket bevorzugen
    if parser_data and f_sides and n_sides:
        pf = parser_data.get("fern") or {}
        pn = parser_data.get("naehe") or {}
        p_f = sum(1 for s in ("rechts", "links") if (pf.get(s) or {}).get("sph"))
        p_n = sum(1 for s in ("rechts", "links") if (pn.get(s) or {}).get("sph"))
        if p_f >= 2 and p_n == 0:
            for side in ("rechts", "links"):
                pe = pf.get(side)
                if pe and pe.get("sph"):
                    fern[side] = _merge_eye(pe, fern.get(side))
                    naehe[side] = None
            data["fern"], data["naehe"] = fern, naehe
        elif p_n >= 2 and p_f == 0:
            for side in ("rechts", "links"):
                pe = pn.get(side)
                if pe and pe.get("sph"):
                    naehe[side] = _merge_eye(pe, naehe.get(side))
                    fern[side] = None
            data["fern"], data["naehe"] = fern, naehe

    return data


def _parse_pass_date(text: str) -> str | None:
    for pat in [
        r"(?:Gültig|Gueltig|gültig)\s+ab\s*[:.]?\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})",
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


def _fill_eye_block_from_matches(text: str, pattern: re.Pattern) -> dict[str, dict | None]:
    block: dict[str, dict | None] = {"rechts": None, "links": None}
    for m in pattern.finditer(text):
        side = _side_key(m.group(1))
        add = m.group(5) if m.lastindex and m.lastindex >= 5 else None
        eye = _eye_from_parts(m.group(2), m.group(3), m.group(4), add)
        if eye:
            block[side] = eye
    return block


def _fill_naehe_from_matches(text: str, pattern: re.Pattern) -> dict[str, dict | None]:
    return _fill_eye_block_from_matches(text, pattern)


def _mcoptic_pass_buckets(eyes: dict[str, dict | None]) -> tuple[dict, dict]:
    """McOptic-Karte: Einstärke/Ferne wenn Add≈0, sonst Nähe (Lesewert)."""
    if any(_add_is_near((eyes.get(s) or {}).get("add")) for s in ("rechts", "links")):
        return _empty_eye_block(), eyes
    return eyes, _empty_eye_block()


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
        "fielmann_brillenpass",
        gueltig_ab=_parse_pass_date(text),
        naehe=naehe,
        glas={"beschreibung": glas_desc, "index": index, "durchmesser": None, "beschichtungen": []},
    )


def parse_mcoptic_pass(ocr_text: str) -> dict:
    """McOptic Brillenpass-Karte (SPH ZYL ACHSE ADD PD)."""
    text = ocr_text or ""
    eyes = _fill_eye_block_from_matches(text, _MCOPTIC_RL)
    fern, naehe = _mcoptic_pass_buckets(eyes)

    glas_desc = ""
    for pat in [
        r"(?:Glas|Lens|Brille)\s*[:.]?\s*(.+?)(?:\n|R\s*:|Rechts)",
        r"(\d{4}RX\s+[\w\s]+)",
        r"(Inside|Desk|Progressive|Office|Comfort\s+SV)\s+[\w\s\d]+",
    ]:
        gm = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if gm:
            glas_desc = re.sub(r"\s+", " ", (gm.group(1) if gm.lastindex else gm.group(0)).strip())[:500]
            break

    return _bp_base(
        "mcoptic_brillenpass",
        gueltig_ab=_parse_pass_date(text),
        fern=fern,
        naehe=naehe,
        glas={"beschreibung": glas_desc, "index": None, "durchmesser": None, "beschichtungen": []},
    )


_MCOPTIC_RECHNUNG_RL = re.compile(
    r"(?:^|\n)\s*(R|L|Rechts|Links)\s*[:.]?\s*"
    r"(?:Sph\.?\s*)?"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"(?:Cyl\.?|Zyl\.?)\s*"
    r"([+\-]?\s*[\d.,]+)\s+"
    r"(?:A°?|Achse|Axis)\s*"
    r"(\d+)"
    r"(?:\s+(?:Add\.?|Addition)\s*([+\-]?\s*[\d.,]+))?",
    re.IGNORECASE,
)


def parse_mcoptic_rechnung(ocr_text: str) -> dict:
    """McOptic Quittung / Krankenkassenexemplar (Messungstabelle)."""
    text = ocr_text or ""
    fern = _fill_naehe_from_matches(text, _MCOPTIC_RECHNUNG_RL)

    glas_desc = ""
    for pat in [
        r"Optische Sonnengläser\s+(.+?)(?:\n|Upgrade)",
        r"Upgrade\s+(.+?)(?:\n|Verlaufend|Brillenschutz)",
        r"Ralph\s+[\w\d\s\-]+(\d{2}-\d{2})",
    ]:
        gm = re.search(pat, text, re.IGNORECASE)
        if gm:
            glas_desc = re.sub(r"\s+", " ", gm.group(0).strip())[:500]
            break

    rechnung = ""
    rm = re.search(r"Quittung\s*No:?\s*([\w\-]+)", text, re.IGNORECASE)
    if rm:
        rechnung = rm.group(1).strip()

    return _bp_base(
        "mcoptic_rechnung",
        gueltig_ab=_parse_pass_date(text),
        fern=fern,
        naehe={"rechts": None, "links": None},
        rechnung=rechnung,
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
        "augenarzt_verordnung",
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
    base["parser"] = "optik_meyer_rechnung"
    base["extraktion"]["quelle"] = "optik_meyer_rechnung_regex"

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
    "fielmann_rechnung": parse_fielmann_brillenpass,
    "fielmann_brillenpass": parse_fielmann_pass,
    "mcoptic_brillenpass": parse_mcoptic_pass,
    "mcoptic_rechnung": parse_mcoptic_rechnung,
    "augenarzt_verordnung": parse_augenarzt,
    "optik_meyer_rechnung": parse_optik_meyer_moehlin,
}


def parse_by_parser(parser_name: str, ocr_text: str) -> dict | None:
    fn = _PARSERS.get(normalize_parser_name(parser_name))
    if not fn:
        return None
    return fn(ocr_text or "")


def _detect_fielmann_rechnung(text: str) -> int:
    score = 0
    if re.search(r"Nähe\s+Rechts|Naehe\s+Rechts", text, re.I):
        score += 3
    if re.search(r"Brillenglas|Asph\.?\s*Hochbr|Gesamtbetrag", text, re.I):
        score += 2
    if re.search(r"Fielmann", text, re.I):
        score += 1
    return score


def _detect_fielmann_brillenpass(text: str) -> int:
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


def _detect_mcoptic_brillenpass(text: str) -> int:
    score = 0
    if re.search(r"Mc\s*Optic|McOptic", text, re.I):
        score += 3
    if re.search(r"SPH\s+ZYL|ZYL\s+ACHSE", text, re.I):
        score += 2
    if _MCOPTIC_RL.search(text):
        score += 3
    if re.search(r"Quittung|Krankenkassenexemplar", text, re.I):
        score -= 2
    return max(0, score)


def _detect_mcoptic_rechnung(text: str) -> int:
    score = 0
    if re.search(r"Mc\s*Optic|McOptic", text, re.I):
        score += 3
    if re.search(r"Quittung|Krankenkassenexemplar|Messungsart", text, re.I):
        score += 2
    if _MCOPTIC_RECHNUNG_RL.search(text):
        score += 3
    return score


def _detect_augenarzt_verordnung(text: str) -> int:
    score = 0
    if re.search(r"Verordnung|Augenarzt|Augenärzt|Dioptrie|Refraktion", text, re.I):
        score += 2
    if _AUGENARZT_RL.search(text):
        score += 3
    if re.search(r"Fielmann|Mc\s*Optic|Optik\s+Meyer", text, re.I):
        score -= 1
    return max(0, score)


def _detect_optik_meyer_rechnung(text: str) -> int:
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
    "fielmann_rechnung": _detect_fielmann_rechnung,
    "fielmann_brillenpass": _detect_fielmann_brillenpass,
    "mcoptic_brillenpass": _detect_mcoptic_brillenpass,
    "mcoptic_rechnung": _detect_mcoptic_rechnung,
    "augenarzt_verordnung": _detect_augenarzt_verordnung,
    "optik_meyer_rechnung": _detect_optik_meyer_rechnung,
}


def looks_like_brillenpass_document(
    ocr_text: str, parser_name: str, dokumenttyp_visuell: str = "",
) -> bool:
    """Parser-spezifische Erkennung (Pass, Verordnung, Rechnung)."""
    name = normalize_parser_name(parser_name or "")
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
