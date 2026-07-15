"""Tests für Legacy QR-Split."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from legacy_split_by_qr import (  # noqa: E402
    LEGACY_QR_REGEX_SPACE,
    QR_REGEX_PRESETS,
    UnsafeRegexError,
    _match_barcode,
    find_split_markers,
    has_real_qr_splits,
    resolve_legacy_qr_regex,
    validate_user_regex,
)
import re


def test_match_barcode_embedded():
    pat = re.compile(r"^[0-9]{6}_[^\s]+$")
    assert _match_barcode("060102_Gesundheit_Monika", pat) == "060102_Gesundheit_Monika"
    assert _match_barcode("prefix 010401_Lohn_Monika suffix", pat) == "010401_Lohn_Monika"
    assert _match_barcode("QR:060102_X", pat) is None or _match_barcode("060102_X", pat)


def test_match_barcode_space_format():
    pat = re.compile(LEGACY_QR_REGEX_SPACE)
    assert _match_barcode("060101 Gesundheit Thomas", pat) == "060101 Gesundheit Thomas"
    assert _match_barcode("020101 Rg allgemein", pat) == "020101 Rg allgemein"
    multiline = "SPC\n0200\n060101 Gesundheit Thomas\nCH"
    assert _match_barcode(multiline, pat) == "060101 Gesundheit Thomas"


def test_resolve_legacy_qr_regex_presets():
    assert resolve_legacy_qr_regex(preset="underscore") == QR_REGEX_PRESETS["underscore"][0]
    assert resolve_legacy_qr_regex(preset="space") == LEGACY_QR_REGEX_SPACE
    try:
        resolve_legacy_qr_regex(preset="invalid")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_has_real_qr_splits():
    assert has_real_qr_splits([("Kein_Barcode", 1), ("060102_A", 3)])
    assert not has_real_qr_splits([("Kein_Barcode", 1)])


def test_validate_user_regex_ok():
    assert validate_user_regex(r"^[0-9]{6}_[^\s]+$") == r"^[0-9]{6}_[^\s]+$"


def test_validate_user_regex_rejects_nested_quantifier():
    try:
        validate_user_regex(r"(a+)+")
        assert False, "expected UnsafeRegexError"
    except UnsafeRegexError:
        pass
