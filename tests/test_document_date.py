"""Tests für generische Ausstellungsdatum-Extraktion."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document_date import (  # noqa: E402
    extract_document_issue_date,
    resolve_issue_date,
    validate_issue_date,
)


def test_ort_und_datum():
    text = "Total Beiträge\nOrt und Datum\nZürich, 19.01.2026"
    iso, src = extract_document_issue_date(text)
    assert iso == "2026-01-19"
    assert "ort_und_datum" in src


def test_erstellt_am_monat():
    text = "Steuerwertbescheinigung\nErstellt am 3. Februar 2026"
    iso, src = extract_document_issue_date(text)
    assert iso == "2026-02-03"
    assert "erstellt" in src


def test_datum_ort():
    text = "Datum Aarau, 7. November 2025"
    iso, _ = extract_document_issue_date(text)
    assert iso == "2025-11-07"


def test_basel_den():
    text = "Fielmann AG\nBasel, den 19.06.2026"
    iso, src = extract_document_issue_date(text)
    assert iso == "2026-06-19"
    assert "ort_komma" in src


def test_excludes_geburtsdatum():
    text = "Geburtsdatum 10.05.1974\nOrt und Datum Zürich, 19.01.2026"
    iso, _ = extract_document_issue_date(text, exclude_iso_dates={"1974-05-10"})
    assert iso == "2026-01-19"


def test_frick_den_maerz():
    """McOptic Quittung: «Frick, den 22. März 2022»."""
    text = "McOptic\nFrick, den 22. März 2022\nQuittung No: Q-TEST"
    iso, src = extract_document_issue_date(text)
    assert iso == "2022-03-22"
    assert "ort_komma_monat" in src


def test_validate_rejects_old():
    iso, susp = validate_issue_date("1974-05-10", 2026, set())
    assert iso is None


def test_resolve_prefers_vision_llm_over_suspicious_ocr():
    """OCR misreads UID/phone noise as 2008; Vision+LLM agree on 2026."""
    ocr = (
        "Datum: 26.09.08\n"
        "CHE-348.352.613\n"
        "Rechnung XXXLutz Marketplace"
    )
    iso, src, susp = resolve_issue_date(
        ocr,
        "2026-09-08",
        "2026-09-08",
        2026,
        set(),
    )
    assert iso == "2026-09-08"
    assert src == "vision+llm"
    assert not susp


def test_resolve_uses_clean_ocr_when_not_suspicious():
    text = "Ort und Datum\nZürich, 19.01.2026"
    iso, src, susp = resolve_issue_date(text, None, None, 2026, set())
    assert iso == "2026-01-19"
    assert "ort_und_datum" in src
    assert not susp


def test_resolve_discards_suspicious_ocr_without_vision_llm():
    ocr = "Datum: 26.09.08\nnoise"
    iso, src, _ = resolve_issue_date(ocr, None, None, 2026, set())
    assert iso is None
    assert src == ""
