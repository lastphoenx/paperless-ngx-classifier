"""Tests für Legacy-Split Publish."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from correspondent_manager_app import _legacy_split_publish_parts  # noqa: E402


def test_legacy_split_publish_atomic(tmp_path):
    staging = tmp_path / "staging"
    consume = tmp_path / "consume"
    staging.mkdir()
    part = staging / "teil.pdf"
    part.write_bytes(b"%PDF-1.4 test content for split")

    published = _legacy_split_publish_parts(
        [{"path": str(part), "filename": "ocrscan_test_p1_bis_p1.pdf", "barcode": "x", "from_page": 1, "to_page": 1}],
        consume,
    )
    dest = consume / "ocrscan_test_p1_bis_p1.pdf"
    assert dest.is_file()
    assert not (consume / "ocrscan_test_p1_bis_p1.pdf.part").exists()
    assert published[0]["path"] == str(dest)
