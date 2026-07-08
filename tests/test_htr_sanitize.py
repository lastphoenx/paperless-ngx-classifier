"""HTR-Zeilenbereinigung, Content-Strategie D und Ausgabe-Format."""
from handwriting_vision import (
    HTR_CONTENT_MARKER,
    build_htr_content_append,
    extract_htr_searchable_text,
    format_htr_note_summary,
)
from schulbericht_vision import (
    HTR_PAGE_MARKER,
    build_page_marked_transcript,
    clean_htr_lines,
    is_htr_junk_line,
    merge_htr_transcribe_pages,
    transcript_for_metadata_extract,
)


def test_is_htr_junk_line_filters_placeholders():
    assert is_htr_junk_line("...")
    assert is_htr_junk_line("handschrift_zeilen")
    assert is_htr_junk_line("SCHULBERICHT")
    assert is_htr_junk_line("Schuljahr:")
    assert is_htr_junk_line("für Thomas Sa")
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


def test_merge_htr_transcribe_pages_keeps_per_page_text():
    pages = [
        {
            "gedruckt": ["SCHULBERICHT"],
            "handschrift_zeilen": ["Seite eins Text."],
        },
        {
            "handschrift_zeilen": ["Seite zwei Text.", "Seite zwei Text."],
        },
    ]
    merged = merge_htr_transcribe_pages(pages, pages_total=2)
    assert merged["seiten_texte"] == ["Seite eins Text.", "Seite zwei Text."]
    assert "..." not in merged["volltext"]


def test_build_page_marked_transcript():
    text = build_page_marked_transcript(["Alpha", "Beta"])
    assert HTR_PAGE_MARKER.format(n=1) in text
    assert HTR_PAGE_MARKER.format(n=2) in text
    assert "Alpha" in text
    assert "Beta" in text


def test_transcript_for_metadata_extract_page1_only():
    seiten = [
        "für Thomas Santinelli\nSchuljahr 1997/98\nLehrperson: C. Die",
        "Fortsetzung Seite zwei — ignorieren für Kopfdaten.",
    ]
    out = transcript_for_metadata_extract(seiten, max_lines=5)
    assert "Santinelli" in out
    assert "Seite zwei" not in out


def test_extract_htr_searchable_text_strategy_d():
    meta = {
        "htr_profile": "schulbericht",
        "_schulbericht": {
            "schueler_vorname": "Thomas",
            "schueler_nachname": "Santinelli",
            "klasse": "1 Kl.",
            "semester_oder_zeitraum": "1997/98",
            "arbeits_haltung": "Nur in Notiz.",
            "leistungen": "Nur in Notiz.",
            "_htr": {
                "seiten_texte": [
                    "Seite 1 Handschrift.",
                    "Seite 2 Handschrift.",
                ],
            },
        },
    }
    text = extract_htr_searchable_text(meta)
    assert "Schüler: Thomas Santinelli" in text
    assert "Zeitraum: 1997/98" in text
    assert "Arbeitshaltung:" not in text
    assert "Leistungen:" not in text
    assert HTR_PAGE_MARKER.format(n=1) in text
    assert HTR_PAGE_MARKER.format(n=2) in text
    assert "Seite 1 Handschrift." in text
    assert "Seite 2 Handschrift." in text


def test_build_htr_content_append_drops_ocr():
    ocr = "Tesseract Müll\nZeile zwei"
    htr = f"{HTR_CONTENT_MARKER}\nSchüler: Max"
    out = build_htr_content_append(ocr, "Schüler: Max", drop_ocr=True)
    assert out.startswith(HTR_CONTENT_MARKER)
    assert "Tesseract" not in out
    assert "Schüler: Max" in out


def test_format_htr_note_summary_compact():
    meta = {
        "htr_profile": "schulbericht",
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
    assert "Arbeitshaltung: Kurz." in note
    assert "X" * 100 not in note
