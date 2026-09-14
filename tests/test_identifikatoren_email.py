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
        'Von: "Berater, Anna" <berater@example-bank.com>\n'
        f'An: "{_HH_USER1}" <{_HH_USER1}>\n'
        "Datum: 20.05.2026"
    )
    found = pc._extract_corr_emails_from_text(text)
    assert found == ["berater@example-bank.com"]


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
                "name": "Berater, Anna",
                "identifikatoren": {"email": ["berater@example-bank.com"]},
            }
        ]
    }
    text = 'Von: "Berater, Anna" <berater@example-bank.com>'
    entry, grund = pc._match_correspondent_by_identifikatoren(corr_map, text)
    assert entry["name"] == "Berater, Anna"
    assert grund == "E-Mail"


def test_ch_phone_format_extracted():
    text = "Kontakt: +41-31-358 64 33\n"
    vorschlag = pc._extract_identifikatoren_vorschlag(text)
    assert vorschlag["telefon"]
    assert pc._norm_corr_telefon(vorschlag["telefon"][0]) == "41313586433"


def test_ch_phone_with_trunk_zero_extracted():
    text = "Tel: +41 (0) 61 971 89 80\n"
    vorschlag = pc._extract_identifikatoren_vorschlag(text)
    assert vorschlag["telefon"]
    assert pc._norm_corr_telefon(vorschlag["telefon"][0]) == "41619718980"


def test_an_recipient_filtered_even_without_family(monkeypatch):
    monkeypatch.setattr(pc, "_load_family", lambda: {"personen": []})
    text = (
        'Von: "Berater, Anna" <berater@example-bank.com>\n'
        f'An: "{_HH_USER1}" <{_HH_USER1}>\n'
    )
    found = pc._extract_corr_emails_from_text(text)
    assert found == ["berater@example-bank.com"]


def test_normalize_email_backend():
    from correspondent_manager_app import _normalize_identifikatoren  # noqa: E402

    out = _normalize_identifikatoren({
        "email": ["  Kontakt@Firma.CH  ", "kontakt@firma.ch", "invalid"],
        "uid": [],
        "iban": [],
        "telefon": [],
        "website": [],
    })
    assert out["email"] == ["kontakt@firma.ch"]


def test_match_by_website():
    corr_map = {
        "eintraege": [
            {
                "name": "Betty Bossi",
                "match": ["betty bossi"],
                "identifikatoren": {"website": ["bettybossi.ch"]},
            },
            {
                "name": "JUMBO",
                "identifikatoren": {"uid": ["CHE-116.311.185"]},
            },
        ]
    }
    text = "Rechnung\nwww.bettybossi.ch\nCHE-116.311.185 MWST"
    entry, grund = pc._match_correspondent_by_identifikatoren(
        corr_map, text, absender="Betty Bossi",
    )
    assert entry["name"] == "Betty Bossi"
    assert "Website" in grund
    assert entry["name"] == "Betty Bossi"


def test_uid_loses_to_vision_absender():
    corr_map = {
        "eintraege": [
            {
                "name": "JUMBO",
                "match": ["jumbo"],
                "identifikatoren": {"uid": ["CHE-116.311.185"]},
            },
            {
                "name": "Betty Bossi",
                "match": ["betty bossi"],
            },
        ]
    }
    text = "CHE-116.311.185 MWST\nSlush Maschine"
    entry, grund = pc._match_correspondent_by_identifikatoren(
        corr_map, text, absender="Betty Bossi",
        vision_meta={"logo_vorhanden": True},
    )
    assert entry["name"] == "Betty Bossi"
    assert "Logo/Absender" in grund


def test_shared_uid_weak_tie():
    coop_uid = "CHE-116.311.185"
    corr_map = {
        "eintraege": [
            {"name": "Coop", "identifikatoren": {"uid": [coop_uid]}},
            {"name": "JUMBO", "identifikatoren": {"uid": [coop_uid]}},
        ]
    }
    text = f"Rechnung {coop_uid} MWST"
    entry, grund = pc._match_correspondent_by_identifikatoren(corr_map, text)
    assert entry is None
    assert grund == ""


def test_multi_signal_beats_uid_alone():
    corr_map = {
        "eintraege": [
            {
                "name": "Betty Bossi",
                "match": ["betty bossi"],
                "identifikatoren": {"website": ["bettybossi.ch"]},
            },
            {
                "name": "JUMBO",
                "identifikatoren": {"uid": ["CHE-116.311.185"]},
            },
        ]
    }
    text = "www.bettybossi.ch\nCHE-116.311.185"
    entry, grund = pc._match_correspondent_by_identifikatoren(
        corr_map, text, absender="Betty Bossi", vision_meta={"logo_vorhanden": True},
    )
    assert entry["name"] == "Betty Bossi"
    assert "Website" in grund
    assert "Logo/Absender" in grund


def test_extract_website_vorschlag():
    text = "Kontakt: www.bettybossi.ch\n"
    vorschlag = pc._extract_identifikatoren_vorschlag(text)
    assert "bettybossi.ch" in vorschlag["website"]


def test_normalize_website_backend():
    from correspondent_manager_app import _normalize_identifikatoren  # noqa: E402

    out = _normalize_identifikatoren({
        "website": ["https://www.BettyBossi.ch/shop", "www.bettybossi.ch"],
        "uid": [],
        "iban": [],
        "swift": [],
        "email": [],
        "telefon": [],
    })
    assert out["website"] == ["bettybossi.ch"]
