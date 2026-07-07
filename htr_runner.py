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


def _correspondent_entry_from_doc(doc: dict, pc) -> tuple[dict | None, str | None]:
    corr_id = doc.get("correspondent")
    if not corr_id:
        return None, None
    try:
        pl_corr = pc.paperless_get(f"/correspondents/{corr_id}/")
        corr_name = (pl_corr.get("name") or "").strip()
    except Exception:
        return None, None
    if not corr_name:
        return None, None
    corr_map = pc._load_corr_map()
    entry = pc._resolve_corr_entry(corr_map, corr_name)
    return entry, corr_name


def _remove_pending_htr_decision(document_id: int) -> None:
    import post_consume as pc  # noqa: WPS433

    path = pc.PENDING_HTR_DECISION_PATH
    if not path.exists():
        return
    kept = []
    for ln in path.read_text(encoding="utf-8").split("\n"):
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
            if row.get("document_id") != document_id:
                kept.append(ln)
        except json.JSONDecodeError:
            kept.append(ln)
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    try:
        tag_id = pc._get_by_name("tags", pc.PENDING_HTR_DECISION_TAG)
        if tag_id:
            doc = pc.paperless_get(f"/documents/{document_id}/")
            tags = [t for t in (doc.get("tags") or []) if t != tag_id]
            pc.paperless_patch(f"/documents/{document_id}/", {"tags": tags})
    except Exception as e:
        log.warning("pending_htr_decision Tag entfernen fehlgeschlagen: %s", e)


def reprocess_htr_document(
    document_id: int,
    *,
    profile_override: str = "",
) -> dict:
    """HTR für bestehendes Dokument. Schreibt Paperless-Notiz mit Transkript."""
    import post_consume as pc  # noqa: WPS433
    from handwriting_vision import (  # noqa: WPS433
        HTR_ACTION_RUN,
        HtrPipelineDeps,
        decide_htr_action,
        format_htr_note_summary,
        normalize_document_type_key,
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
    corr_entry, corr_name = _correspondent_entry_from_doc(doc, pc)
    dt_key, _ = normalize_document_type_key(doctype_name) if doctype_name else (None, "paperless")

    resolution = decide_htr_action(
        vision_meta,
        ocr_text,
        explicit=profile_override or None,
        correspondent=corr_entry,
        correspondent_match="paperless" if corr_entry else None,
        document_type_key=dt_key,
        document_type_source="paperless",
    )
    pc.write_audit_entry(document_id, "htr_pre_resolution", resolution.to_audit_dict())

    if resolution.action != HTR_ACTION_RUN:
        return {
            "ok": False,
            "error": (
                f"Kein HTR-Profil (action={resolution.action}, source={resolution.htr_profile_source}). "
                "Profil manuell wählen oder Dokumenttyp/Korrespondent prüfen."
            ),
        }

    deps = HtrPipelineDeps(
        pdf_to_b64=pc._schulbericht_pdf_to_b64,
        htr_page=pc.vision_htr_page,
        schulbericht_page_e2e=pc.vision_schulbericht_page,
        extract_schulbericht=pc.extract_schulbericht_from_transcript,
    )
    htr_meta = run_htr_pipeline(resolution, pdf_path, ocr_text, deps)
    if not htr_meta:
        return {
            "ok": False,
            "error": f"HTR-Profil '{resolution.profile_name}' lieferte kein Ergebnis",
        }

    if resolution.variants:
        pc.write_audit_entry(document_id, "htr", {
            **resolution.to_audit_dict(),
            "variants": resolution.variants,
            "correspondent": corr_name,
        })

    note_body = format_htr_note_summary(htr_meta)
    try:
        pc.paperless_post_note(document_id, note_body)
    except Exception as e:
        log.warning("HTR-Notiz für #%s fehlgeschlagen: %s", document_id, e)

    _remove_pending_htr_decision(document_id)

    conf = htr_meta.get("htr_confidence") or htr_meta.get("schulbericht_confidence")
    return {
        "ok": True,
        "document_id": document_id,
        "profile": resolution.profile_name,
        "confidence": conf,
        "message": f"HTR ({resolution.profile_name}) abgeschlossen — Notiz am Dokument",
        "htr_meta": htr_meta,
    }


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="HTR für bestehendes Paperless-Dokument")
    ap.add_argument("document_id", type=int)
    ap.add_argument("--profile", default="", help="Profil aus htr_profiles.json (sonst auto)")
    args = ap.parse_args()
    out = reprocess_htr_document(args.document_id, profile_override=args.profile)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if out.get("ok") else 1)
