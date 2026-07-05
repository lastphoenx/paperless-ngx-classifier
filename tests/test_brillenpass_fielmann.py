"""Tests für Fielmann Brillenpass-Parser (anonymes OCR-Beispiel)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brillenpass_parser import (  # noqa: E402
    has_brillenpass_values,
    looks_like_optiker_rechnung,
    merge_brillenpass,
    parse_ch_date_short,
    parse_fielmann_brillenpass,
)

FIELMANN_OCR = """
Fielmann AG - Marktplatz 16 - 4001 Basel
Basel, den 19.06.25
Ihr Auftrag AUF-123456 vom 05.06.25
Rechnung: RE-9876543210
Wir lieferten Ihnen gemäss Brillenglasbestimmung CHF Betrag
Glas: Durchmesser 75, Asph.Hochbr.Kst.1.6+Blaufil., Raum-Comf. weit.
Sph Cyl Achse Prisma Basis Add
Nähe Rechts: + 0.50 - 0.50 65 1.75 A 307.00
Nähe Links: + 0.25 - 0.50 105 1.75 A 307.00
Gesamtbetrag 633.00
"""


def test_looks_like_optiker():
    assert looks_like_optiker_rechnung(FIELMANN_OCR, "Rechnung")


def test_parse_date():
    assert parse_ch_date_short("Basel, den 19.06.25") == "2025-06-19"


def test_parse_fielmann_naehe():
    r = parse_fielmann_brillenpass(FIELMANN_OCR)
    assert r["naehe"]["rechts"]["sph"] == "+0.50"
    assert r["naehe"]["rechts"]["cyl"] == "-0.50"
    assert r["naehe"]["rechts"]["achse"] == "65"
    assert r["naehe"]["rechts"]["add"] == "+1.75"
    assert r["naehe"]["rechts"]["prisma"] is None
    assert r["naehe"]["links"]["sph"] == "+0.25"
    assert r["glas"]["index"] == "1.6"
    assert r["glas"]["durchmesser"] == 75
    assert "Blaufilter" in r["glas"]["beschichtungen"]
    assert "0558" in r["auftrag"]
    assert r["gueltig_ab"] == "2025-06-19"
    assert has_brillenpass_values(r)


def test_merge_vision_fills_gap():
    parser = parse_fielmann_brillenpass(FIELMANN_OCR)
    vision = {
        "fern": {
            "rechts": {"sph": "+1.00", "cyl": "-0.25", "achse": "90", "prisma": None, "basis": None, "add": None},
            "links": None,
        },
        "glas": {"index": None},
    }
    merged = merge_brillenpass(parser, vision)
    assert merged["naehe"]["rechts"]["sph"] == "+0.50"
    assert merged["fern"]["rechts"]["sph"] == "+1.00"
