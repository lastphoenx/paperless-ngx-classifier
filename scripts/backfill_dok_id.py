#!/usr/bin/env python3
"""
Einmalig CF «Dok-ID» (Paperless document id) für alle Dokumente nachbefüllen.

Voraussetzung: Custom Field in Paperless angelegt, CF_DOK_ID in .env gesetzt.

  python scripts/backfill_dok_id.py --dry-run
  python scripts/backfill_dok_id.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Nur zählen, nicht patchen")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--sleep", type=float, default=0.15, help="Pause zwischen PATCH (Sek.)")
    args = ap.parse_args()

    api_url = os.environ.get("PAPERLESS_API_URL", "http://localhost:8000/api").rstrip("/")
    token = os.environ.get("PAPERLESS_TOKEN") or os.environ.get("PAPERLESS_API_TOKEN", "")
    cf_id = int(os.environ.get("CF_DOK_ID", "0"))

    if not token:
        print("FEHLER: PAPERLESS_TOKEN nicht gesetzt", file=sys.stderr)
        return 1
    if not cf_id:
        print("FEHLER: CF_DOK_ID nicht gesetzt (0 = deaktiviert)", file=sys.stderr)
        return 1

    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }

    updated = skipped = errors = 0
    url = f"{api_url}/documents/"
    params: dict = {"page_size": args.page_size, "ordering": "id"}

    while url:
        r = requests.get(url, headers=headers, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        for doc in data.get("results", []):
            doc_id = doc["id"]
            cfs = {cf["field"]: cf["value"] for cf in doc.get("custom_fields", []) if cf.get("field")}
            if cfs.get(cf_id) == doc_id:
                skipped += 1
                continue
            merged = [{"field": fid, "value": val} for fid, val in cfs.items() if fid != cf_id]
            merged.append({"field": cf_id, "value": doc_id})
            if args.dry_run:
                updated += 1
                continue
            try:
                pr = requests.patch(
                    f"{api_url}/documents/{doc_id}/",
                    headers=headers,
                    json={"custom_fields": merged},
                    timeout=30,
                )
                pr.raise_for_status()
                updated += 1
                if args.sleep:
                    time.sleep(args.sleep)
            except Exception as e:
                errors += 1
                print(f"FEHLER Dok #{doc_id}: {e}", file=sys.stderr)

        url = data.get("next")
        params = {}

    mode = "würde setzen" if args.dry_run else "gesetzt"
    print(f"Fertig: {updated} {mode}, {skipped} bereits ok, {errors} Fehler (CF #{cf_id})")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
