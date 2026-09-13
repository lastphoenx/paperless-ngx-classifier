"""Tests für swift_utils."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swift_utils import extract_swifts_from_text, normalize_swift  # noqa: E402


def test_normalize_swift():
    assert normalize_swift("BKBBCHBB") == "BKBBCHBB"
    assert normalize_swift("bkbb chbb") == "BKBBCHBB"


def test_extract_swift_labeled():
    text = "Bankverbindung\nSWIFT: BKBBCHBB\nIBAN CH71 0077..."
    found = extract_swifts_from_text(text)
    assert "BKBBCHBB" in found


def test_rejects_german_invoice_words():
    text = (
        "Ihre RECHNUNG zur Bestellung\n"
        "Wenn Sie Fragen haben brauchen Sie das Kontaktcenter\n"
        "Ausgestellt von XLCH AG\n"
        "XXXLutz MARKETPLACE\nUNZUFRIEDEN?"
    )
    found = extract_swifts_from_text(text)
    assert "RECHNUNG" not in found
    assert "MARKETPLACE" not in found
    assert "UNZUFRIEDEN" not in found
    assert "BRAUCHEN" not in found
    assert "AUSGESTELLT" not in found


def test_standalone_requires_bank_context():
    text = "Bezahlte Rechnung BRAUCHEN Sie BKBBCHBB Hilfe"
    assert extract_swifts_from_text(text, standalone_requires_bank_context=True) == []
    text2 = "Bankverbindung IBAN CH12 3456 SWIFT BKBBCHBB"
    found = extract_swifts_from_text(text2, standalone_requires_bank_context=True)
    assert "BKBBCHBB" in found


def test_accepts_real_swiss_bic_standalone():
    text = "Bank UBSWCHZH80A\nZahlung an UBS"
    found = extract_swifts_from_text(text)
    assert "UBSWCHZH80A" in found
