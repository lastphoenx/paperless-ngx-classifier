#!/usr/bin/env python3
"""brillenpaesse.json reparieren: messung hydrieren + bekannte Dokument-Korrekturen."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brillenpass_parser import (  # noqa: E402
    apply_brillenpass_doc_patches,
    compute_brillenpass_diff,
    dedupe_brillenpass_versions_by_document,
    hydrate_messung_from_diagnose,
    resolve_brillenpass_aktuell,
    sort_brillenpass_versions,
)

BP_PATH = Path(os.environ.get(
    "BRILLENPAESSE_JSON",
    "/opt/paperless-scripts/training/brillenpaesse.json",
))


def repair_store(data: dict) -> int:
    fixes = 0
    for entry in data.get("eintraege", []):
        vers = list(entry.get("versionen") or [])
        for version in vers:
            if hydrate_messung_from_diagnose(version):
                fixes += 1
            if apply_brillenpass_doc_patches(version):
                fixes += 1
        deduped, dedup_changed = dedupe_brillenpass_versions_by_document(vers)
        if dedup_changed:
            fixes += 1
            vers = deduped
            for i, v in enumerate(vers):
                prev = vers[i - 1] if i > 0 else None
                v["diff_zu_vorher"] = compute_brillenpass_diff(prev, v)
            entry["versionen"] = vers
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
