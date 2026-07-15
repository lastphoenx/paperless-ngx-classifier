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
