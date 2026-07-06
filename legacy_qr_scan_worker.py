#!/usr/bin/env python3
"""
Legacy QR-Scan — Subprocess-Worker (Hauptthread, wie legacy_qr_split_test.py).

Aufruf von correspondent-manager im Thread-Pool: pyzbar/libzbar darf nicht
im Worker-Thread laufen (Deadlock). Child-Prozess = gleicher Pfad wie CLI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from legacy_split_by_qr import (  # noqa: E402
    DEFAULT_DPI,
    DEFAULT_QR_REGEX,
    find_split_markers,
    normalize_legacy_qr_regex,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Legacy QR scan worker (JSON stdout)")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    regex = normalize_legacy_qr_regex(os.environ.get("LEGACY_SPLIT_QR_REGEX", DEFAULT_QR_REGEX))
    backends = ("ghostscript",) if args.quick else None
    dpis = (150, 300) if args.quick else None
    markers, total, qr_debug, scan_meta = find_split_markers(
        str(args.pdf),
        regex=regex,
        dpi=DEFAULT_DPI,
        backends=backends,
        dpis=dpis,
    )
    json.dump(
        {
            "markers": markers,
            "total": total,
            "qr_debug": qr_debug,
            "scan_meta": scan_meta,
        },
        sys.stdout,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
