"""Tests für Steuerjahr-Inferenz."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steuerjahr import infer_steuerjahr  # noqa: E402


def test_lohnausweis_year_in_title():
    assert infer_steuerjahr(
        title="Lohnausweis 2025",
        doctyp_name="Lohnausweis",
    ) == 2025


def test_saldo_per_december():
    text = "Kontoauszug\nSaldo per 31.12.2025 CHF 12'345.00"
    assert infer_steuerjahr(ocr_text=text, doctyp_name="Kontoauszug") == 2025


def test_3a_january_issue_previous_year():
    assert infer_steuerjahr(
        ocr_text="Säule 3a Einzahlungsbescheinigung",
        ausstellungsdatum="2026-02-15",
        doctyp_name="Korrespondenz",
    ) == 2025


def test_rechnung_uses_issue_year():
    assert infer_steuerjahr(
        ocr_text="Rechnung Heizöl",
        ausstellungsdatum="2025-08-12",
        doctyp_name="Rechnung",
    ) == 2025
