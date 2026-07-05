"""Tests für Brillenpass-Parser (Pass, Augenarzt, Optiker)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brillenpass_parser import (  # noqa: E402
    chronological_prev_version,
    corr_brillenpass_parsers,
    detect_parser,
    has_brillenpass_values,
    latest_brillenpass_version,
    merge_brillenpass,
    merge_brillenpass_version,
    normalize_parser_name,
    normalize_vision_brillenpass,
    resolve_brillenpass_aktuell,
    sort_brillenpass_versions,
    _merge_eye,
    parse_augenarzt,
    parse_by_parser,
    parse_brillenpass_auto,
    parse_fielmann_pass,
    parse_mcoptic_pass,
    parse_mcoptic_rechnung,
    parse_optik_meyer_moehlin,
    diagnose_brillenpass_extraction,
    find_brillenpass_period_duplicate,
    should_trigger_brillenpass,
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

MCOPTIC_RECHNUNG_OCR = """
McOptic Frick
Quittung No: Q-TEST-2022
Frick, den 22. März 2022
Optische Sonnengläser Einstärken Bronze
TOTAL inkl. MwSt. 440.00 CHF
Messungsart: Brillenkorrektur Ferne
R Sph. -1.00 Cyl. -0.50 A° 90
L Sph. -1.25 Cyl. -0.75 A° 85
"""

MCOPTIC_PASS_FERN = """
McOptic Basel
Patientin Beispiel
SPH ZYL ACHSE ADD PD
R -1.00 -0.50 90 0.00 30.0
L -1.25 -0.75 85 0.00 31.0
Gültig ab 15.03.2022
Comfort SV Demo Glas
"""

MCOPTIC_PASS_2012_OCR = """
Patientin Beispiel
26.04.2012
ORGA 150 HMC MAX
SPH ZYL ACHSE PRISMA BAS ADD PD
R -3.00 -1.00 176 0 0 0 29.5
L -1.00 -1.25 6 0 0 0 31.5
MCOPTIK (SCHWEIZ) AG / LIESTAL
"""

MCOPTIC_RECHNUNG_TABLE_OCR = """
McOptic Frick
Thomas Beispiel
14.10.2023
SPH ZYL ACHSE ADD PD
R +0.25 -0.25 57 1.50 31.5
L 0.00 -0.25 110 1.50 31.5
Quittung No: Q-TEST-2023
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


def test_parser_aliases():
    assert normalize_parser_name("fielmann") == "fielmann_rechnung"
    assert normalize_parser_name("mcoptic_pass") == "mcoptic_brillenpass"


def test_corr_vendor_parsers():
    entry = {"brillenpass": {"aktiv": True, "vendor": "mcoptic"}}
    assert set(corr_brillenpass_parsers(entry)) == {
        "mcoptic_rechnung", "mcoptic_brillenpass",
    }


def test_fielmann_pass():
    r = parse_fielmann_pass(FIELMANN_PASS_OCR)
    assert r["parser"] == "fielmann_brillenpass"
    assert r["naehe"]["rechts"]["sph"] == "+0.25"
    assert r["naehe"]["rechts"]["add"] == "+1.25"
    assert r["naehe"]["links"]["achse"] == "105"
    assert r["gueltig_ab"] == "2019-09-10"
    assert "Zeiss" in r["glas"]["beschreibung"]
    assert has_brillenpass_values(r)


def test_mcoptic_pass():
    r = parse_mcoptic_pass(MCOPTIC_PASS_OCR)
    assert r["parser"] == "mcoptic_brillenpass"
    assert r["naehe"]["rechts"]["add"] == "+1.50"
    assert r["naehe"]["links"]["cyl"] == "-0.50"
    assert r["pd"]["rechts"] == "31.5"
    assert r["pd"]["links"] == "32.0"
    assert r["gueltig_ab"] == "2023-10-15"
    assert has_brillenpass_values(r)


def test_mcoptic_pass_fern_einstaerke():
    r = parse_mcoptic_pass(MCOPTIC_PASS_FERN)
    assert r["fern"]["rechts"]["sph"] == "-1.00"
    assert r["fern"]["rechts"]["cyl"] == "-0.50"
    assert r["fern"]["rechts"]["achse"] == "90"
    assert r["fern"]["links"]["sph"] == "-1.25"
    assert r["naehe"]["rechts"] is None
    assert r["pd"]["rechts"] == "30.0"
    assert r["pd"]["links"] == "31.0"
    assert r["gueltig_ab"] == "2022-03-15"
    assert "Comfort" in r["glas"]["beschreibung"]


def test_mcoptic_pass_2012_pd_with_empty_columns():
    r = parse_mcoptic_pass(MCOPTIC_PASS_2012_OCR)
    assert r["fern"]["rechts"]["sph"] == "-3.00"
    assert r["fern"]["rechts"]["cyl"] == "-1.00"
    assert r["fern"]["rechts"]["achse"] == "176"
    assert r["fern"]["links"]["cyl"] == "-1.25"
    assert r["pd"]["rechts"] == "29.5"
    assert r["pd"]["links"] == "31.5"
    assert r["gueltig_ab"] == "2012-04-26"


def test_merge_prefer_vision_fills_cyl_and_pd():
    parser = {
        "fern": {"rechts": {"sph": "-2.00", "cyl": None, "achse": "176"}, "links": None},
        "naehe": {"rechts": None, "links": None},
        "pd": {"rechts": None, "links": None},
    }
    vision = {
        "fern": {
            "rechts": {"sph": "-3.00", "cyl": "-1.00", "achse": "176"},
            "links": {"sph": "-1.00", "cyl": "-1.25", "achse": "6"},
        },
        "pd": {"rechts": "29.5", "links": "31.5"},
    }
    m = merge_brillenpass(parser, vision, prefer_vision=True)
    assert m["fern"]["rechts"]["cyl"] == "-1.00"
    assert m["fern"]["rechts"]["sph"] == "-2.00"
    assert m["pd"]["rechts"] == "29.5"
    assert m["pd"]["links"] == "31.5"


def test_should_trigger_brillenpass_optiker_keywords():
    entry = {"brillenpass": {"aktiv": True, "vendor": "mcoptic"}}
    names = corr_brillenpass_parsers(entry)
    assert should_trigger_brillenpass("Messungsart: Brillenkorrektur Ferne\nR Sph -1.00", names)
    assert not should_trigger_brillenpass("Steuerbescheid 2024", names)


def test_mcoptic_rechnung_table_naehe_both_eyes():
    """Rechnung mit Pass-Tabelle (ADD) → beide Augen Nähe."""
    r = parse_mcoptic_rechnung(MCOPTIC_RECHNUNG_TABLE_OCR)
    assert r["parser"] == "mcoptic_rechnung"
    assert r["naehe"]["rechts"]["sph"] == "+0.25"
    assert r["naehe"]["rechts"]["cyl"] == "-0.25"
    assert r["naehe"]["rechts"]["achse"] == "57"
    assert r["naehe"]["links"]["sph"] == "+0.00"
    assert r["naehe"]["links"]["achse"] == "110"
    assert r["pd"]["rechts"] == "31.5"
    assert r["pd"]["links"] == "31.5"


def test_diagnose_brillenpass_gaps():
    parser = parse_mcoptic_pass(MCOPTIC_PASS_2012_OCR)
    vision = {"fern": {"rechts": {"sph": "-3.00", "achse": "176"}, "links": None}, "naehe": {"rechts": None, "links": None}}
    merged = merge_brillenpass(parser, vision, prefer_vision=True)
    d = diagnose_brillenpass_extraction(parser, vision, merged, parser_detected="mcoptic_brillenpass", has_image=True)
    assert "fern.rechts.cyl" in d["gaps"] or "naehe.rechts.cyl" in d["gaps"]


def test_consolidate_near_bucket_moves_fern_to_naehe():
    merged = merge_brillenpass(
        {"fern": {"rechts": {"sph": "+0.25", "cyl": "-0.25", "achse": "57", "add": "+1.50"}, "links": None},
         "naehe": {"rechts": None, "links": {"sph": "+0.00", "cyl": "-0.25", "achse": "110", "add": "+1.50"}}},
        {},
    )
    assert merged["naehe"]["rechts"]["sph"] == "+0.25"
    assert merged["naehe"]["links"]["sph"] == "+0.00"
    assert merged["fern"]["rechts"] is None


def test_merge_mcoptic_split_vision():
    """Vision verteilt R→fern, L→naehe; Parser hat beides in fern."""
    parser = parse_mcoptic_pass(MCOPTIC_PASS_FERN)
    vision = {
        "fern": {
            "rechts": {"sph": "1.00", "cyl": "-0.50", "achse": "90", "basis": "R", "add": None},
            "links": None,
        },
        "naehe": {
            "rechts": None,
            "links": {"sph": "-1.25", "cyl": "-0.75", "achse": "85", "basis": "L", "add": None},
        },
    }
    m = merge_brillenpass(parser, vision)
    assert m["fern"]["rechts"]["sph"] == "-1.00"
    assert m["fern"]["links"]["sph"] == "-1.25"
    assert m["fern"]["rechts"].get("basis") is None
    assert m["naehe"]["links"] is None


def test_vision_only_plus_sph_not_auto_negated():
    """Kein pauschales Minus nur wegen negativem Cyl (echte Weitsicht + bleibt)."""
    m = _merge_eye(None, {"sph": "+2.50", "cyl": "-0.50", "achse": "90"})
    assert m["sph"] == "+2.50"


def test_merge_sph_sign_conflict_prefers_parser():
    p = {"sph": "-2.50", "cyl": "-1.25", "achse": "178"}
    v = {"sph": "2.50", "cyl": "-1.25", "achse": "178"}
    m = _merge_eye(p, v)
    assert m["sph"] == "-2.50"


def test_brillenpass_version_chronology():
    vers = [
        {"gueltig_ab": "2022-03-15", "korrespondent": "McOptic"},
        {"gueltig_ab": "2012-04-26", "korrespondent": "McOptic"},
    ]
    assert resolve_brillenpass_aktuell(vers) == "2022-03-15"
    assert latest_brillenpass_version(vers)["gueltig_ab"] == "2022-03-15"
    prev = chronological_prev_version(vers[:1], "2012-04-26")
    assert prev is None
    prev2 = chronological_prev_version(vers, "2023-01-01")
    assert prev2["gueltig_ab"] == "2022-03-15"
    assert find_brillenpass_period_duplicate(vers, "2012-04-26", "McOptic") is None


def test_gueltig_ab_ch_format():
    from brillenpass_parser import _parse_gueltig_date, normalize_gueltig_ab_iso
    assert _parse_gueltig_date("22.03.2022").isoformat() == "2022-03-22"
    assert normalize_gueltig_ab_iso("22.03.2022") == "2022-03-22"


def test_merge_brillenpass_version_collects_document_ids():
    existing = {
        "gueltig_ab": "2022-03-15",
        "document_id": 1001,
        "document_ids": [1001],
        "fern": {"rechts": {"sph": "-1.00"}, "links": None},
        "naehe": {"rechts": None, "links": None},
        "glas": {},
    }
    incoming = {
        "document_id": 1002,
        "document_ids": [1002],
        "naehe": {"rechts": None, "links": {"sph": "-1.25"}},
        "glas": {"beschreibung": "Comfort Demo"},
    }
    m = merge_brillenpass_version(existing, incoming)
    assert m["document_ids"] == [1001, 1002]
    assert m["document_id"] == 1001
    assert m["extraktion"]["dedup_merged"] is True


def test_augenarzt():
    r = parse_augenarzt(AUGENARZT_OCR)
    assert r["parser"] == "augenarzt_verordnung"
    assert r["naehe"]["rechts"]["sph"] == "+0.50"
    assert r["naehe"]["rechts"]["add"] == "+1.50"
    assert r["gueltig_ab"] == "2025-03-28"


def test_optik_meyer_rechnung():
    r = parse_optik_meyer_moehlin(MEYER_OCR)
    assert r["parser"] == "optik_meyer_rechnung"
    assert r["naehe"]["rechts"]["add"] == "+1.25"
    assert "2022" in (r.get("rechnung") or "2022")
    assert has_brillenpass_values(r)


def test_detect_parser_format():
    mcoptic_allowed = ["mcoptic_rechnung", "mcoptic_brillenpass"]
    assert detect_parser(MCOPTIC_PASS_OCR, allowed=mcoptic_allowed) == "mcoptic_brillenpass"
    assert detect_parser(MCOPTIC_RECHNUNG_OCR, allowed=mcoptic_allowed) == "mcoptic_rechnung"
    assert detect_parser(
        MCOPTIC_RECHNUNG_OCR,
        allowed=mcoptic_allowed,
        dokumenttyp_visuell="Rechnung / Quittung A4",
    ) == "mcoptic_rechnung"
    assert detect_parser(FIELMANN_PASS_OCR) == "fielmann_brillenpass"
    assert detect_parser(MCOPTIC_PASS_OCR) == "mcoptic_brillenpass"
    assert detect_parser(AUGENARZT_OCR) == "augenarzt_verordnung"
    assert detect_parser(MEYER_OCR) == "optik_meyer_rechnung"


def test_parse_brillenpass_auto():
    r = parse_brillenpass_auto(MCOPTIC_PASS_OCR, ["mcoptic_rechnung", "mcoptic_brillenpass"])
    assert r["parser"] == "mcoptic_brillenpass"
    assert r["extraktion"]["parser_detected"] == "mcoptic_brillenpass"


def test_parse_by_parser():
    r = parse_by_parser("mcoptic_pass", MCOPTIC_PASS_OCR)
    assert r["parser"] == "mcoptic_brillenpass"
    assert r["naehe"]["rechts"]["add"] == "+1.50"


def test_normalize_vision_moves_pd_from_prisma():
    raw = {
        "fern": {
            "rechts": {"sph": "-2.75", "cyl": "-1.25", "achse": "179", "prisma": "29.5", "basis": "ADD", "add": "3"},
            "links": {"sph": "-1.00", "cyl": "-1.50", "achse": "0"},
        },
        "pd": {"rechts": None, "links": "null"},
    }
    norm = normalize_vision_brillenpass(raw)
    assert norm["pd"]["rechts"] == "29.5"
    assert norm["pd"]["links"] is None
    assert norm["fern"]["rechts"].get("add") is None
    assert norm["fern"]["rechts"].get("basis") is None


def test_normalize_vision_moves_pd_from_basis():
    raw = {
        "fern": {
            "rechts": {"sph": "+0.25", "cyl": "-0.25", "achse": "57", "prisma": "1.50"},
            "links": {"sph": "0.00", "cyl": "-0.25", "achse": "110", "prisma": "1.50", "basis": "31.5"},
        },
        "pd": {"rechts": None, "links": None},
    }
    norm = normalize_vision_brillenpass(raw)
    assert norm["fern"]["rechts"].get("add") == "+1.50"
    assert norm["pd"]["links"] == "31.5"


def test_consolidate_ignores_hallucinated_integer_add():
    """Einstärke ohne ADD: Vision-«3» darf nicht nach naehe verschieben."""
    vision = {
        "fern": {
            "rechts": {"sph": "-2.75", "cyl": "-1.25", "achse": "179", "add": "3"},
            "links": {"sph": "-1.00", "cyl": "-1.50", "achse": "0"},
        },
        "pd": {"rechts": "29.5", "links": "31.0"},
    }
    norm = normalize_vision_brillenpass(vision)
    merged = merge_brillenpass({}, norm, prefer_vision=True)
    assert merged["fern"]["rechts"]["sph"] == "-2.75"
    assert merged["naehe"]["rechts"] is None
    assert merged["pd"]["links"] == "31.0"


def test_mcoptic_header_date():
    text = "26-32910 / Monika Santinelli\n12.06.2015\n10.06.2015\nSPH ZYL ACHSE\nR -2.75 -1.25 179 29.5"
    from brillenpass_parser import _parse_pass_date
    assert _parse_pass_date(text) == "2015-06-10"


def test_diagnose_no_add_gap_for_fern_only():
    merged = {
        "fern": {
            "rechts": {"sph": "-2.75", "cyl": "-1.25", "achse": "179"},
            "links": {"sph": "-1.00", "cyl": "-1.50", "achse": "0"},
        },
        "naehe": {"rechts": None, "links": None},
        "pd": {"rechts": "29.5", "links": "31.0"},
        "gueltig_ab": "2015-06-10",
    }
    d = diagnose_brillenpass_extraction({}, {}, merged, has_image=True)
    assert "fern.rechts.add" not in d["gaps"]
    assert "fern.links.add" not in d["gaps"]
    assert "gueltig_ab" not in d["gaps"]


def test_consolidate_near_bucket_still_moves_real_add():
    merged = merge_brillenpass(
        {"fern": {"rechts": {"sph": "+0.25", "cyl": "-0.25", "achse": "57", "add": "+1.50"}, "links": None},
         "naehe": {"rechts": None, "links": {"sph": "+0.00", "cyl": "-0.25", "achse": "110", "add": "+1.50"}}},
        {},
    )
    assert merged["naehe"]["rechts"]["sph"] == "+0.25"
    assert merged["fern"]["rechts"] is None
