"""HTR-Zeilenbereinigung und Ausgabe-Format."""
from handwriting_vision import extract_htr_searchable_text, format_htr_note_summary
from schulbericht_vision import clean_htr_lines, is_htr_junk_line, merge_htr_transcribe_pages


def test_is_htr_junk_line_filters_placeholders():
    assert is_htr_junk_line("...")
    assert is_htr_junk_line("handschrift_zeilen")
    assert is_htr_junk_line("SCHULBERICHT")
    assert not is_htr_junk_line("Thomas rechnet gut im allgemeinen")


def test_clean_htr_lines_dedupes_and_drops_boilerplate():
    raw = [
        "SCHULBERICHT",
        "...",
        "handschrift_zeilen",
        "Thomas rechnet gut.",
        "Thomas rechnet gut.",
        "Der Beförderungsentscheid ist im Zeugnis eingetragen.",
        "In letzter Zeit zeigt Thomas öfters Unlust.",
    ]
    out = clean_htr_lines(raw)
    assert "SCHULBERICHT" not in out
    assert "..." not in out
    assert out.count("Thomas rechnet gut.") == 1
    assert not any("Beförderungsentscheid" in x for x in out)
    assert "In letzter Zeit zeigt Thomas öfters Unlust." in out


def test_merge_htr_transcribe_pages_cleans_output():
    pages = [
        {
            "gedruckt": ["SCHULBERICHT", "..."],
            "handschrift_zeilen": ["Thomas liest langsam.", "..."],
        },
        {
            "handschrift_zeilen": ["Thomas liest langsam.", "Diktate schreibt er fast fehlerfrei."],
        },
    ]
    merged = merge_htr_transcribe_pages(pages, pages_total=2)
    assert "..." not in merged["volltext"]
    assert "handschrift_zeilen" not in merged["volltext"]
    assert merged["volltext"].count("Thomas liest langsam.") == 1


def test_extract_htr_searchable_text_no_volltext_dump():
    meta = {
        "htr_profile": "schulbericht",
        "_schulbericht": {
            "schueler_vorname": "Thomas",
            "schueler_nachname": "Sandulli",
            "klasse": "1 Kl.",
            "arbeits_haltung": "Arbeitet meist gut.",
            "leistungen": "Rechnen gut.",
            "_htr": {
                "handschrift_zeilen": ["Zeile A", "..."],
                "volltext": "SCHULBERICHT\n...\nZeile A\nZeile A",
            },
        },
    }
    text = extract_htr_searchable_text(meta)
    assert "Schüler: Thomas Sandulli" in text
    assert "Arbeitshaltung: Arbeitet meist gut." in text
    assert "handschrift_zeilen" not in text
    assert text.count("Zeile A") == 1
    assert len(text) < 500


def test_format_htr_note_summary_compact():
    meta = {
        "htr_profile": "schulbericht_crop_strong",
        "schulbericht_confidence": 0.58,
        "_schulbericht": {
            "schueler_vorname": "Thomas",
            "schueler_nachname": "Santinelli",
            "arbeits_haltung": "Kurz.",
            "leistungen": "Gut.",
            "_htr": {"volltext": "X" * 5000},
        },
    }
    note = format_htr_note_summary(meta)
    assert "Confidence: 0.58" in note
    assert "Vorname: Thomas" in note
    assert "X" * 100 not in note
