"""Tests für E-Mail-Identifikator (Korrespondent-Matching)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import post_consume as pc  # noqa: E402


def _patch_household(monkeypatch, emails):
    monkeypatch.setattr(
        pc,
        "_load_family",
        lambda: {"personen": [{"email": e} for e in emails]},
    )


def test_extract_email_from_von_header(monkeypatch):
    _patch_household(monkeypatch, ["user1@example.com"])
    text = (
        'Von: "Hagmann, Marlene" <marlene.hagmann@ubs.com>\n'
        'An: "user1@example.com" <user1@example.com>\n'
        "Datum: 20.05.2026"
    )
    found = pc._extract_corr_emails_from_text(text)
    assert found == ["marlene.hagmann@ubs.com"]


def test_household_recipient_filtered(monkeypatch):
    _patch_household(monkeypatch, ["user1@example.com", "monika@example.ch"])
    text = (
        "From: user1@example.com\n"
        "An: monika@example.ch\n"
        "kontakt@ubs.com"
    )
    found = pc._extract_corr_emails_from_text(text)
    assert "user1@example.com" not in found
    assert "monika@example.ch" not in found
    assert "kontakt@ubs.com" in found


def test_match_by_email(monkeypatch):
    _patch_household(monkeypatch, ["user1@example.com"])
    corr_map = {
        "eintraege": [
            {
                "name": "Hagmann, Marlene",
                "identifikatoren": {"email": ["marlene.hagmann@ubs.com"]},
            }
        ]
    }
    text = 'Von: "Hagmann, Marlene" <marlene.hagmann@ubs.com>'
    entry, grund = pc._match_correspondent_by_identifikatoren(corr_map, text)
    assert entry["name"] == "Hagmann, Marlene"
    assert grund == "E-Mail"


def test_ch_phone_format_extracted():
    text = "Kontakt: +41-31-358 64 33\n"
    vorschlag = pc._extract_identifikatoren_vorschlag(text)
    assert vorschlag["telefon"]
    assert pc._norm_corr_telefon(vorschlag["telefon"][0]) == "41313586433"


def test_an_recipient_filtered_even_without_family(monkeypatch):
    monkeypatch.setattr(pc, "_load_family", lambda: {"personen": []})
    text = (
        'Von: "Hagmann, Marlene" <marlene.hagmann@ubs.com>\n'
        'An: "user1@example.com" <user1@example.com>\n'
    )
    found = pc._extract_corr_emails_from_text(text)
    assert found == ["marlene.hagmann@ubs.com"]


def test_normalize_email_backend():
    from correspondent_manager_app import _normalize_identifikatoren  # noqa: E402

    out = _normalize_identifikatoren({
        "email": ["  Kontakt@Firma.CH  ", "kontakt@firma.ch", "invalid"],
        "uid": [],
        "iban": [],
        "telefon": [],
    })
    assert out["email"] == ["kontakt@firma.ch"]
