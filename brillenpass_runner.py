"""
Nachträgliche Brillenpass-Pipeline für bestehende Paperless-Dokumente.
Wird von paper.manager API und optional als CLI genutzt.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("brillenpass_runner")


def reprocess_brillenpass_document(
    document_id: int,
    *,
    force: bool = False,
    parser_override: str = "",
) -> dict:
    """
    Bestehendes Dokument durch Brillenpass-Parser + Vision schicken.
    Returns: {ok, message, document_id, person_id?, ...}
    """
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    import post_consume as pc  # noqa: WPS433

    try:
        doc = pc.paperless_get(f"/documents/{document_id}/")
    except Exception as e:
        return {"ok": False, "error": f"Dokument #{document_id} nicht gefunden: {e}"}

    ocr_text = doc.get("content") or ""
    if not ocr_text.strip():
        return {"ok": False, "error": "Kein OCR-Text — Dokument zuerst in Paperless indexieren"}

    corr_pl_id = doc.get("correspondent")
    corr_entry = None
    if corr_pl_id:
        try:
            pl_corr = pc.paperless_get(f"/correspondents/{corr_pl_id}/")
            corr_name = pl_corr.get("name", "")
            corr_map = pc._load_corr_map()
            corr_entry = next(
                (e for e in corr_map.get("eintraege", [])
                 if e.get("name", "").lower() == corr_name.lower()),
                None,
            )
            if not corr_entry:
                return {"ok": False, "error": f"Korrespondent '{corr_name}' nicht in correspondents.json"}
        except Exception as e:
            return {"ok": False, "error": f"Korrespondent laden fehlgeschlagen: {e}"}
    else:
        return {"ok": False, "error": "Dokument hat keinen Korrespondenten"}

    parser_names = pc.corr_brillenpass_parsers(corr_entry)
    if parser_override:
        from brillenpass_parser import normalize_parser_name
        parser_names = [
            normalize_parser_name(p)
            for p in parser_override.replace(",", " ").split()
            if p.strip()
        ]
    if not parser_names and not parser_override:
        aktiv, _ = pc.corr_supports_brillenpass(corr_entry)
        if not aktiv:
            return {
                "ok": False,
                "error": (
                    f"Korrespondent «{corr_entry.get('name', '?')}» ohne brillenpass.aktiv "
                    f"— in paper.manager → Korrespondenten "
                    f'"brillenpass": {{"aktiv": true, "vendor": "fielmann"}}'
                ),
            }

    pdf_path = pc.resolve_document_pdf(document_id)
    image_b64 = pc.pdf_to_base64_image(pdf_path) if pdf_path else None
    if not image_b64:
        log.warning(
            "Brillenpass Trigger #%s: kein PDF-Bild (MEDIA_ROOT=%s) — Stufe 2 übersprungen",
            document_id, pc.MEDIA_ROOT,
        )

    vision_meta = {"dokumenttyp_visuell": "", "datum": (doc.get("created") or "")[:10] or None}
    dt_vis = vision_meta.get("dokumenttyp_visuell", "")
    if not pc.should_trigger_brillenpass(ocr_text, parser_names, dt_vis, vision_meta):
        detected = pc.detect_parser(
            ocr_text, allowed=parser_names, dokumenttyp_visuell=dt_vis, vision_meta=vision_meta,
        )
        if detected and not parser_override:
            parser_names = pc.corr_brillenpass_parsers(corr_entry) or [detected]
            log.info("Brillenpass: Parser auto-erkannt → %s", detected)
        elif not parser_override:
            return {"ok": False, "error": f"Kein Brillenpass-Dokument erkannt (Kandidaten: {parser_names})"}

    direct_name, direct_reason = pc._match_person_direct(ocr_text, vision_meta)
    if not direct_name:
        return {"ok": False, "error": "Person nicht eindeutig (family.json / OCR)"}

    person_id = pc._resolve_person_id(direct_name)
    anzeigename = pc._resolve_person_anzeigename(person_id) or direct_name

    parser_data = pc.parse_brillenpass_with_parsers(
        ocr_text, parser_names, dokumenttyp_visuell=dt_vis, vision_meta=vision_meta,
    )
    if not parser_data and "fielmann_rechnung" in parser_names:
        parser_data = pc.parse_fielmann_brillenpass(ocr_text)
    chosen = pc.detect_parser(
        ocr_text, allowed=parser_names, dokumenttyp_visuell=dt_vis, vision_meta=vision_meta,
    )
    if document_id:
        pc.write_audit_entry(document_id, "brillenpass_s1", {
            "parser": chosen, "snapshot": pc.snapshot_brillenpass(parser_data),
        })
    vision_bp = pc.vision_brillenpass_analyze(image_b64, ocr_text, parser_data)
    if document_id:
        pc.write_audit_entry(document_id, "brillenpass_s2", {
            "has_image": bool(image_b64),
            "snapshot": pc.snapshot_brillenpass(vision_bp),
            "vision_empty": not vision_bp,
        })
    prefer_vis = bool(image_b64)
    merged = pc.merge_brillenpass(parser_data, vision_bp, prefer_vision=prefer_vis)
    from brillenpass_parser import diagnose_brillenpass_extraction  # noqa: WPS433
    diagnose = diagnose_brillenpass_extraction(
        parser_data, vision_bp, merged,
        parser_detected=chosen, has_image=bool(image_b64), prefer_vision=prefer_vis,
    )
    merged.setdefault("extraktion", {})["diagnose"] = diagnose
    if document_id:
        pc.write_audit_entry(document_id, "brillenpass_merged", diagnose)
    if diagnose.get("gaps"):
        log.warning("Brillenpass Lücken #%s: %s", document_id, ", ".join(diagnose["gaps"]))
    merged["korrespondent"] = corr_entry.get("name", "")
    if not merged.get("gueltig_ab") and doc.get("created"):
        merged["gueltig_ab"] = str(doc["created"])[:10]

    if not pc.has_brillenpass_values(merged):
        return {"ok": False, "error": "Keine verwertbaren Brillenpass-Werte extrahiert"}

    if force:
        _remove_pending_brillenpass(document_id, pc.PENDING_BRILLENPASS_PATH)

    queued = pc.write_pending_brillenpass(
        merged, person_id, anzeigename, corr_entry.get("name", ""), document_id=document_id,
    )
    if not queued:
        return {
            "ok": False,
            "error": "Bereits in Review-Queue — «Erneut» mit force=true",
            "document_id": document_id,
        }

    tag_id = pc._get_by_name("tags", pc.PENDING_BRILLENPASS_TAG) or pc._create_obj("tags", pc.PENDING_BRILLENPASS_TAG)
    if tag_id:
        try:
            tags = list(doc.get("tags") or [])
            if tag_id not in tags:
                tags.append(tag_id)
                pc.paperless_patch(document_id, {"tags": tags})
        except Exception as e:
            log.warning("Brillenpass-Tag setzen fehlgeschlagen: %s", e)

    return {
        "ok": True,
        "message": f"Brillenpass-Review eingereiht für {anzeigename}",
        "document_id": document_id,
        "person_id": person_id,
        "parser": parser_names,
        "person_match": direct_reason,
    }


def _remove_pending_brillenpass(document_id: int, path: Path) -> None:
    if not path.exists():
        return
    kept = []
    for ln in path.read_text(encoding="utf-8").split("\n"):
        if not ln.strip():
            continue
        try:
            e = json.loads(ln)
            if e.get("document_id") == document_id and e.get("status") == "pending":
                continue
        except json.JSONDecodeError:
            pass
        kept.append(ln)
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Brillenpass-Pipeline für bestehendes Dokument")
    ap.add_argument("document_id", type=int)
    ap.add_argument("--force", action="store_true", help="Bestehenden pending-Eintrag ersetzen")
    ap.add_argument("--parser", default="", help="Parser-Override (z. B. mcoptic_brillenpass)")
    args = ap.parse_args()
    result = reprocess_brillenpass_document(
        args.document_id, force=args.force, parser_override=args.parser,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)
