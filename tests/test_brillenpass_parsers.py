"""Tests für Brillenpass-Parser (Pass, Augenarzt, Optiker)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brillenpass_parser import (  # noqa: E402
    detect_parser,
    has_brillenpass_values,
    parse_augenarzt,
    parse_by_parser,
    parse_fielmann_pass,
    parse_mcoptic_pass,
    parse_optik_meyer_moehlin,
)

FIELMANN_PASS_OCR = """
Fielmann AG Brillenpass
Datum: 10.09.19
R: S +0.25 C -0.25 A 65 ADD 1.25
L: S +0.25 C -0.25 A 105 ADD 1.25
Glas: Zeiss Single Vision
"""

MCOPTIC_PASS_OCR = """
McOptic Basel
SPH ZYL ACHSE ADD PD
R: +0.25 -0.25 57 1.50 31.5
L: +0.25 -0.50 105 1.50 32.0
Datum: 15.10.23
Inside Lens Kulanz
"""

AUGENARZT_OCR = """
Augenarztpraxis Dr. Beispiel
Verordnung vom 28.03.2025
Rechts: sph +0.50 cyl -0.25 axis 65 add +1.50
Links: sph +0.25 cyl -0.50 axis 105 add +1.50
"""

MEYER_OCR = """
Optik Meyer Möhlin
Möhlin, den 22.03.2022
Rechnung Nr. 2022/1847
Total CHF 890.00
Rechts: +0.25 -0.25 65 1.25
Links: +0.25 -0.50 105 1.25
Glasart: Einstärke 1.6
"""


def test_fielmann_pass():
    r = parse_fielmann_pass(FIELMANN_PASS_OCR)
    assert r["naehe"]["rechts"]["sph"] == "+0.25"
    assert r["naehe"]["rechts"]["add"] == "+1.25"
    assert r["naehe"]["links"]["achse"] == "105"
    assert r["gueltig_ab"] == "2019-09-10"
    assert "Zeiss" in r["glas"]["beschreibung"]
    assert has_brillenpass_values(r)


def test_mcoptic_pass():
    r = parse_mcoptic_pass(MCOPTIC_PASS_OCR)
    assert r["naehe"]["rechts"]["add"] == "+1.50"
    assert r["naehe"]["links"]["cyl"] == "-0.50"
    assert r["gueltig_ab"] == "2023-10-15"
    assert has_brillenpass_values(r)


def test_augenarzt():
    r = parse_augenarzt(AUGENARZT_OCR)
    assert r["naehe"]["rechts"]["sph"] == "+0.50"
    assert r["naehe"]["rechts"]["add"] == "+1.50"
    assert r["gueltig_ab"] == "2025-03-28"


def test_optik_meyer_moehlin():
    r = parse_optik_meyer_moehlin(MEYER_OCR)
    assert r["parser"] == "optik_meyer_moehlin"
    assert r["naehe"]["rechts"]["add"] == "+1.25"
    assert "2022" in (r.get("rechnung") or "2022")
    assert has_brillenpass_values(r)


def test_detect_parser():
    assert detect_parser(FIELMANN_PASS_OCR) == "fielmann_pass"
    assert detect_parser(MCOPTIC_PASS_OCR) == "mcoptic_pass"
    assert detect_parser(AUGENARZT_OCR) == "augenarzt"
    assert detect_parser(MEYER_OCR) == "optik_meyer_moehlin"


def test_parse_by_parser():
    r = parse_by_parser("mcoptic_pass", MCOPTIC_PASS_OCR)
    assert r["parser"] == "mcoptic_pass"
    assert r["naehe"]["rechts"]["add"] == "+1.50"
