#!/usr/bin/env python3
"""brillenpaesse.json reparieren: messung hydrieren + bekannte Dokument-Korrekturen."""
from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brillenpass_parser import (  # noqa: E402
    compute_brillenpass_diff,
    dedupe_brillenpass_versions_by_document,
    hydrate_messung_from_diagnose,
    sort_brillenpass_versions,
)

BP_PATH = Path(os.environ.get(
    "BRILLENPAESSE_JSON",
    "/opt/paperless-scripts/training/brillenpaesse.json",
))

_EYE_NULL = {"sph": None, "cyl": None, "achse": None, "prisma": None, "basis": None, "add": None}

# document_id → Felder (deep-merge in Version)
DOC_PATCHES: dict[int, dict] = {
    3568: {
        "korrespondent": "Dr. med. Christian Mauch",
        "messung": {
            "rechts": {"sph": "+0.50", "cyl": "0.00", "achse": "0", "prisma": None, "basis": None, "add": None},
            "links": {"sph": "+1.00", "cyl": "-0.50", "achse": "173", "prisma": None, "basis": None, "add": None},
        },
        "extraktion": {"quelle": "manual", "confidence": "hoch", "layout": "messung"},
    },
    3242: {
        "messung": {
            "links": {"sph": "+1.00", "cyl": "-0.50", "achse": "173", "prisma": None, "basis": None, "add": None},
        },
        "glas": {
            "beschreibung": (
                "rechts Optovision GmbH SV F.K 1.5 Hart Super ET Clean 057531 · "
                "links Optovision GmbH SV F.K 1.5 Hart Super ET Clean 596498"
            ),
        },
        "extraktion": {"confidence": "mittel"},
    },
    3567: {
        "messung": {
            "rechts": {"achse": "100"},
            "links": {"achse": "100"},
        },
    },
}


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = deepcopy(v)


def _apply_doc_patch(version: dict) -> bool:
    doc_id = version.get("document_id")
    patch = DOC_PATCHES.get(doc_id)
    if not patch:
        return False
    _deep_merge(version, patch)
    messung = version.setdefault("messung", {"rechts": None, "links": None})
    for side in ("rechts", "links"):
        eye = messung.get(side)
        if isinstance(eye, dict):
            for k, v in _EYE_NULL.items():
                eye.setdefault(k, v)
    if doc_id == 3242:
        ext = version.setdefault("extraktion", {})
        diag = ext.setdefault("diagnose", {})
        for key in ("merged", "stufe1"):
            block = diag.setdefault(key, {})
            block["messung.links"] = messung.get("links")
        gaps = diag.get("gaps") or []
        diag["gaps"] = [g for g in gaps if g != "messung.links.sph"]
    if doc_id == 3567:
        ext = version.setdefault("extraktion", {})
        diag = ext.get("diagnose") or {}
        for key in ("merged", "stufe1", "stufe2"):
            block = diag.get(key) or {}
            for side in ("rechts", "links"):
                eye = block.get(f"messung.{side}")
                if isinstance(eye, dict):
                    eye["achse"] = "100"
    return True


def repair_store(data: dict) -> int:
    fixes = 0
    for entry in data.get("eintraege", []):
        vers = list(entry.get("versionen") or [])
        for version in vers:
            if hydrate_messung_from_diagnose(version):
                fixes += 1
            if _apply_doc_patch(version):
                fixes += 1
        deduped, dedup_changed = dedupe_brillenpass_versions_by_document(vers)
        if dedup_changed:
            fixes += 1
            vers = deduped
            for i, v in enumerate(vers):
                prev = vers[i - 1] if i > 0 else None
                v["diff_zu_vorher"] = compute_brillenpass_diff(prev, v)
            entry["versionen"] = vers
            from brillenpass_parser import resolve_brillenpass_aktuell
            entry["aktuell"] = resolve_brillenpass_aktuell(vers) or entry.get("aktuell")
        elif vers != entry.get("versionen"):
            entry["versionen"] = vers
    return fixes


def main() -> int:
    if not BP_PATH.exists():
        print(f"Nicht gefunden: {BP_PATH}", file=sys.stderr)
        return 1
    data = json.loads(BP_PATH.read_text(encoding="utf-8"))
    n = repair_store(data)
    if n:
        BP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{BP_PATH}: {n} Version(en) repariert")
    else:
        print(f"{BP_PATH}: nichts zu tun")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
