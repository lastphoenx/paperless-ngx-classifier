"""Tests für Tesseract-TSV Anker-Parser (ohne echtes tesseract)."""
from brillenpass_tsv import (
    count_header_anchors,
    gruppiere_nach_top,
    merge_brillenpass_tsv_with_regex,
    parse_by_anchors,
)
from brillenpass_parser import parse_mcoptic_pass


def _mcoptic_tsv_words() -> list[dict]:
    """Simulierte TSV-Geometrie McOptic-Karte — pro Auge eigener Header (Dok #3563)."""
    def w(text, left, top):
        return {"text": text, "left": left, "top": top, "width": 10, "height": 10, "conf": 90}

    return [
        w("R", 80, 100),
        w("SPH", 200, 100),
        w("ZYL", 280, 100),
        w("ACHSE", 360, 100),
        w("PD", 480, 100),
        w("-2.75", 200, 130),
        w("-1.25", 280, 130),
        w("179", 360, 130),
        w("29.5", 480, 130),
        w("L", 80, 160),
        w("SPH", 200, 160),
        w("ZYL", 280, 160),
        w("ACHSE", 360, 160),
        w("PD", 480, 160),
        w("-1.00", 200, 190),
        w("-1.50", 280, 190),
        w("0", 360, 190),
        w("31.0", 480, 190),
    ]


def test_gruppiere_nach_top_tolerance():
    words = [
        {"text": "A", "left": 10, "top": 100},
        {"text": "B", "left": 50, "top": 108},
        {"text": "C", "left": 10, "top": 140},
    ]
    zeilen = gruppiere_nach_top(words, tol=12)
    assert len(zeilen) == 2
    assert len(zeilen[0]) == 2
    assert len(zeilen[1]) == 1


def test_count_header_anchors_mcoptic():
    assert count_header_anchors(_mcoptic_tsv_words()) >= 3


def test_parse_by_anchors_mcoptic_both_pd():
    r = parse_by_anchors(_mcoptic_tsv_words())
    assert r is not None
    assert r["fern"]["rechts"]["sph"] == "-2.75"
    assert r["fern"]["links"]["sph"] == "-1.00"
    assert r["fern"]["links"]["achse"] == "0"
    assert r["pd"]["rechts"] == "29.5"
    assert r["pd"]["links"] == "31.0"


def test_merge_tsv_wins_over_regex():
    tsv = parse_by_anchors(_mcoptic_tsv_words())
    regex = parse_mcoptic_pass("R -2.00 -1.00 90 28.0\nL -0.50 -1.00 0 30.0")
    merged = merge_brillenpass_tsv_with_regex(tsv, regex)
    assert merged["fern"]["rechts"]["sph"] == "-2.75"
    assert merged["pd"]["links"] == "31.0"


def test_parse_positional_without_full_header():
    words = [
        {"text": "R", "left": 80, "top": 130},
        {"text": "-2.75", "left": 200, "top": 130},
        {"text": "-1.25", "left": 280, "top": 130},
        {"text": "179", "left": 360, "top": 130},
        {"text": "29.5", "left": 480, "top": 130},
        {"text": "L", "left": 80, "top": 160},
        {"text": "-1.00", "left": 200, "top": 160},
        {"text": "-1.50", "left": 280, "top": 160},
        {"text": "0", "left": 360, "top": 160},
        {"text": "31.0", "left": 480, "top": 160},
    ]
    from brillenpass_tsv import gruppiere_nach_top, _parse_rl_rows_positional
    r = _parse_rl_rows_positional(gruppiere_nach_top(words))
    assert r is not None
    assert r["pd"]["rechts"] == "29.5"
    assert r["pd"]["links"] == "31.0"


def test_tsv_text_fallback_mcoptic():
    from brillenpass_tsv import _parse_tsv_text_fallback
    words = [
        {"text": "R", "left": 0, "top": 10},
        {"text": "-2.75", "left": 10, "top": 10},
        {"text": "-1.25", "left": 20, "top": 10},
        {"text": "179", "left": 30, "top": 10},
        {"text": "29.5", "left": 40, "top": 10},
        {"text": "L", "left": 0, "top": 20},
        {"text": "-1.00", "left": 10, "top": 20},
        {"text": "-1.50", "left": 20, "top": 20},
        {"text": "0", "left": 30, "top": 20},
        {"text": "31.0", "left": 40, "top": 20},
    ]
    r = _parse_tsv_text_fallback(words)
    assert r is not None
    assert r["pd"]["links"] == "31.0"


def test_parse_by_anchors_insufficient_headers():
    words = [{"text": "SPH", "left": 10, "top": 10, "conf": 90}]
    assert parse_by_anchors(words) is None
    assert count_header_anchors(words) < 3


def test_merge_rl_split_line():
    from brillenpass_tsv import merge_rl_continuation_lines, _numeric_tokens, _row_side
    words = [
        {"text": "R", "left": 80, "top": 100},
        {"text": "-2.75", "left": 200, "top": 118},
        {"text": "-1.25", "left": 280, "top": 118},
        {"text": "179", "left": 360, "top": 118},
        {"text": "29.5", "left": 480, "top": 118},
    ]
    zeilen = merge_rl_continuation_lines(gruppiere_nach_top(words, tol=12))
    assert any(_row_side(z) == "rechts" and len(_numeric_tokens(z)) >= 3 for z in zeilen)


def test_diopter_from_token_rejects_garbage():
    from brillenpass_parser import strict_diopter_token
    assert strict_diopter_token("293") is None
    assert strict_diopter_token("-2.75") == "-2.75"
    assert strict_diopter_token("-1,25") == "-1.25"


def test_quarter_grid_rejects_ocr_garbage():
    from brillenpass_parser import plausible_refraktion_eye
    assert not plausible_refraktion_eye({"sph": "+2.93", "cyl": "+0.23", "achse": "2"})
    assert plausible_refraktion_eye({"sph": "-2.75", "cyl": "-1.25", "achse": "179"})


def test_norm_pd_mm_no_plus():
    from brillenpass_parser import norm_pd_mm
    assert norm_pd_mm("29.5") == "29.5"
    assert norm_pd_mm("+31.0") == "31.0"


def test_cross_eye_rejects_bleed():
    from brillenpass_parser import plausible_brillenpass_data
    bad = {
        "fern": {
            "rechts": {"sph": "+2.93", "cyl": "+0.23", "achse": "2"},
            "links": {"sph": "-1.00", "cyl": "-1.50", "achse": "0"},
        },
    }
    assert not plausible_brillenpass_data(bad)
