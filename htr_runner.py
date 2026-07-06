"""
Nachträgliche Handschrift-Erkennung (HTR) für bestehende Paperless-Dokumente.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone

log = logging.getLogger("htr_runner")

_jobs_lock = threading.Lock()
_jobs: dict[int, dict] = {}


def _job_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def htr_job_set(document_id: int, **fields) -> None:
    with _jobs_lock:
        prev = _jobs.get(document_id, {})
        _jobs[document_id] = {
            **prev,
            **fields,
            "document_id": document_id,
            "updated_at": _job_now(),
        }


def htr_job_get(document_id: int) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(document_id)
        return dict(job) if job else None


def htr_job_run(document_id: int, *, profile_override: str = "") -> None:
    htr_job_set(document_id, status="running", message="HTR läuft (mehrere Minuten möglich)…")
    try:
        result = reprocess_htr_document(document_id, profile_override=profile_override)
        if result.get("ok"):
            htr_job_set(
                document_id,
                status="done",
                message=result.get("message", "HTR abgeschlossen"),
                profile=result.get("profile"),
                confidence=result.get("confidence"),
            )
        else:
            err = result.get("error", "HTR fehlgeschlagen")
            htr_job_set(document_id, status="error", error=err, message=err)
    except Exception as e:
        log.exception("HTR Job #%s", document_id)
        htr_job_set(document_id, status="error", error=str(e), message=str(e))


def _doctype_name_from_doc(doc: dict, pl_get_dt) -> str:
    dt_id = doc.get("document_type")
    if not dt_id:
        return ""
    try:
        dt = pl_get_dt(dt_id)
        return (dt.get("name") or "").strip()
    except Exception:
        return ""


def reprocess_htr_document(
    document_id: int,
    *,
    profile_override: str = "",
) -> dict:
    """HTR für bestehendes Dokument. Schreibt Paperless-Notiz mit Transkript."""
    import post_consume as pc  # noqa: WPS433
    from handwriting_vision import (  # noqa: WPS433
        HtrPipelineDeps,
        format_htr_note_summary,
        resolve_htr_profile,
        run_htr_pipeline,
    )

    try:
        doc = pc.paperless_get(f"/documents/{document_id}/")
    except Exception as e:
        return {"ok": False, "error": f"Dokument #{document_id} nicht lesbar: {e}"}

    pdf_path = pc.resolve_document_pdf(document_id)
    if not pdf_path:
        return {"ok": False, "error": "PDF nicht gefunden"}

    ocr_text = (doc.get("content") or "").strip()[:3000]
    image_b64 = pc.pdf_to_base64_image(pdf_path)
    vision_meta = pc._disambiguate_vision_money_fields(pc.vision_analyze(image_b64, ocr_text))

    doctype_name = _doctype_name_from_doc(
        doc,
        lambda dt_id: pc.paperless_get(f"/document_types/{dt_id}/"),
    )
    profile = resolve_htr_profile(
        vision_meta,
        ocr_text,
        doctype_name=doctype_name,
        explicit=profile_override or None,
    )
    if not profile:
        return {
            "ok": False,
            "error": "Kein HTR-Profil (Dokumenttyp=off oder keine Handschrift erkannt). "
                     "Profil manuell wählen: default oder schulbericht.",
        }

    deps = HtrPipelineDeps(
        pdf_to_b64=pc._schulbericht_pdf_to_b64,
        htr_page=pc.vision_htr_page,
        schulbericht_page_e2e=pc.vision_schulbericht_page,
        extract_schulbericht=pc.extract_schulbericht_from_transcript,
    )
    htr_meta = run_htr_pipeline(profile, pdf_path, ocr_text, deps)
    if not htr_meta:
        return {"ok": False, "error": f"HTR-Profil '{profile}' lieferte kein Ergebnis"}

    note_body = format_htr_note_summary(htr_meta)
    try:
        pc.paperless_post_note(document_id, note_body)
    except Exception as e:
        log.warning("HTR-Notiz für #%s fehlgeschlagen: %s", document_id, e)

    conf = htr_meta.get("htr_confidence") or htr_meta.get("schulbericht_confidence")
    return {
        "ok": True,
        "document_id": document_id,
        "profile": profile,
        "confidence": conf,
        "message": f"HTR ({profile}) abgeschlossen — Notiz am Dokument",
        "htr_meta": htr_meta,
    }


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="HTR für bestehendes Paperless-Dokument")
    ap.add_argument("document_id", type=int)
    ap.add_argument("--profile", default="", help="default | schulbericht (sonst auto)")
    args = ap.parse_args()
    out = reprocess_htr_document(args.document_id, profile_override=args.profile)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if out.get("ok") else 1)
