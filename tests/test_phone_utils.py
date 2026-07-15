"""Tests für phone_utils."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phone_utils import (  # noqa: E402
    extract_phones_from_text,
    norm_phone_for_match,
    preprocess_phone_text,
)


def test_preprocess_swiss_trunk_zero():
    assert "+41 (0) 61" in preprocess_phone_text("Tel: +41 (0) 61 971 89 80")


def test_norm_phone_swiss_international():
    assert norm_phone_for_match("+41-31-358 64 33") == "41313586433"


def test_norm_phone_swiss_with_trunk_zero():
    assert norm_phone_for_match("+41 (0) 61 971 89 80") == "41619718980"


def test_extract_phone_from_label_with_trunk_zero():
    text = "Ihre Ansprechpartnerin\nJeanette Muheim\nTel: +41 (0) 61 971 89 80\n"
    found = extract_phones_from_text(text)
    assert found
    assert norm_phone_for_match(found[0]) == "41619718980"


def test_extract_phone_national_swiss():
    text = "Kontakt 061 266 16 20\n"
    found = extract_phones_from_text(text)
    assert found
    assert norm_phone_for_match(found[0]) == "41612661620"
