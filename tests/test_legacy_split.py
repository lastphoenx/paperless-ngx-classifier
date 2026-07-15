"""Tests für Legacy QR-Split."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from legacy_split_by_qr import (  # noqa: E402
    UnsafeRegexError,
    _match_barcode,
    find_split_markers,
    has_real_qr_splits,
    validate_user_regex,
)
import re


def test_match_barcode_embedded():
    pat = re.compile(r"^[0-9]{6}_[^\s]+$")
    assert _match_barcode("060102_Gesundheit_Monika", pat) == "060102_Gesundheit_Monika"
    assert _match_barcode("prefix 010401_Lohn_Monika suffix", pat) == "010401_Lohn_Monika"
    assert _match_barcode("QR:060102_X", pat) is None or _match_barcode("060102_X", pat)


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
