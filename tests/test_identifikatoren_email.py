"""Tests für E-Mail-Identifikator (Korrespondent-Matching)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import post_consume as pc  # noqa: E402

_HH_USER1 = "user1@example.com"
_HH_USER2 = "user2@example.com"


def _patch_household(monkeypatch, emails):
    monkeypatch.setattr(
        pc,
        "_load_family",
        lambda: {"personen": [{"email": e} for e in emails]},
    )


def test_extract_email_from_von_header(monkeypatch):
    _patch_household(monkeypatch, [_HH_USER1])
    text = (
        'Von: "Hagmann, Marlene" <marlene.hagmann@ubs.com>\n'
        f'An: "{_HH_USER1}" <{_HH_USER1}>\n'
        "Datum: 20.05.2026"
    )
    found = pc._extract_corr_emails_from_text(text)
    assert found == ["marlene.hagmann@ubs.com"]


def test_household_recipient_filtered(monkeypatch):
    _patch_household(monkeypatch, [_HH_USER1, _HH_USER2])
    text = (
        f"From: {_HH_USER1}\n"
        f"An: {_HH_USER2}\n"
        "kontakt@ubs.com"
    )
    found = pc._extract_corr_emails_from_text(text)
    assert _HH_USER1 not in found
    assert _HH_USER2 not in found
    assert "kontakt@ubs.com" in found


def test_match_by_email(monkeypatch):
    _patch_household(monkeypatch, [_HH_USER1])
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
        f'An: "{_HH_USER1}" <{_HH_USER1}>\n'
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
