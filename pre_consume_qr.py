#!/usr/bin/env python3
"""
pre_consume_qr.py — Swiss QR Bill Parser für Paperless-NGX Pre-Consume Pipeline

Läuft nach ocrmypdf im pre_consume.sh.
Extrahiert strukturierte Daten aus Swiss QR-Rechnungen (SIX-Standard) und
schreibt sie als JSON-Sidecar-Datei neben das PDF.

Der post_consume liest qr_meta.json und setzt Custom Fields in Paperless.

Ablauf:
  1. PDF in Bilder konvertieren (pdf2image)
  2. QR-Codes erkennen (pyzbar)
  3. Swiss QR Bill parsen (SIX-Spec)
  4. qr_meta.json schreiben

Ausgabe: <datei>_qr_meta.json (wird von post_consume gelesen und dann gelöscht)

SIX Swiss QR Bill Standard:
https://www.six-group.com/de/products-services/banking-services/payment-standardization/standards/qr-bill.html
"""

import sys
import os
import json
import re
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger("pre_consume_qr")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)

# ── Swiss QR Bill Feld-Positionen (0-basiert) ─────────────────────────────────
# Gemäss SIX-Spezifikation Version 2.0
_QR_HEADER        = 0   # "SPC"
_QR_VERSION       = 1   # "0200"
_QR_CODING        = 2   # "1"
_QR_IBAN          = 3   # IBAN (21 Zeichen, ohne Leerzeichen)
_QR_KREDITOR_TYP  = 4   # S/K/F
_QR_KREDITOR_NAME = 5
_QR_KREDITOR_STR  = 6
_QR_KREDITOR_HNR  = 7   # (bei Typ S: Hausnummer, bei K: leer)
_QR_KREDITOR_PLZ  = 8
_QR_KREDITOR_ORT  = 9
_QR_KREDITOR_LAND = 10
_QR_LEER          = 11  # leer (kombinierte Adresse Platzhalter)
_QR_BETRAG        = 16
_QR_WAEHRUNG      = 17  # CHF oder EUR
_QR_DEBITOR_TYP   = 18
_QR_DEBITOR_NAME  = 19
_QR_DEBITOR_STR   = 20
_QR_DEBITOR_HNR   = 21
_QR_DEBITOR_PLZ   = 22
_QR_DEBITOR_ORT   = 23
_QR_DEBITOR_LAND  = 24
_QR_REF_TYP       = 25  # QRR / SCOR / NON
_QR_REFERENZ      = 26  # 27-stellig bei QRR, ISO 11649 bei SCOR, leer bei NON
_QR_ZUSATZINFO    = 27
_QR_EPD           = 28  # "EPD"


def _safe_get(lines: list, idx: int) -> str:
    """Sicherer Zugriff auf QR-Zeilen — leer wenn Index ausserhalb."""
    if idx < len(lines):
        return lines[idx].strip()
    return ""


def parse_swiss_qr_bill(qr_text: str) -> dict | None:
    """
    Swiss QR Bill parsen.
    Gibt strukturiertes Dict zurück oder None wenn kein gültiger QR-Bill.

    Rückgabe-Keys:
      iban, betrag, waehrung, ref_typ, referenz, zusatzinfo,
      kreditor_name, debitor_name, faellig_bis
    """
    lines = qr_text.strip().split("\n")

    # Mindestlänge und Header prüfen
    if len(lines) < 28:
        return None
    if _safe_get(lines, _QR_HEADER) != "SPC":
        return None
    if _safe_get(lines, _QR_VERSION) not in ("0200", "0100"):
        return None

    result = {
        "source":        "swiss_qr_bill",
        "iban":          _safe_get(lines, _QR_IBAN).replace(" ", ""),
        "kreditor_name": _safe_get(lines, _QR_KREDITOR_NAME),
        "betrag":        _safe_get(lines, _QR_BETRAG) or None,
        "waehrung":      _safe_get(lines, _QR_WAEHRUNG),
        "debitor_name":  _safe_get(lines, _QR_DEBITOR_NAME),
        "ref_typ":       _safe_get(lines, _QR_REF_TYP),
        "referenz":      None,
        "zusatzinfo":    _safe_get(lines, _QR_ZUSATZINFO) or None,
        "faellig_bis":   None,
        "raw_lines":     len(lines),
    }

    # Referenz nur bei QRR oder SCOR
    ref_typ = result["ref_typ"]
    if ref_typ in ("QRR", "SCOR"):
        ref = _safe_get(lines, _QR_REFERENZ)
        # QRR: 27 Ziffern, Prüfziffer validieren
        if ref_typ == "QRR" and re.match(r"^\d{26,27}$", ref.replace(" ", "")):
            result["referenz"] = ref.replace(" ", "")
        elif ref_typ == "SCOR":
            result["referenz"] = ref

    # Fälligkeitsdatum aus AV-Feldern (optionale Zusatzdaten nach EPD)
    # Format: "AV1:UV;REF1;DATUM" oder ähnlich — Datum suchen
    for line in lines[_QR_EPD:]:
        if line.startswith("AV"):
            date_match = re.search(
                r"(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4}|\d{8})", line
            )
            if date_match:
                raw_date = date_match.group(1)
                result["faellig_bis"] = _normalize_date(raw_date)

    # Betrag normalisieren (Komma → Punkt, als Float)
    if result["betrag"]:
        try:
            result["betrag"] = float(result["betrag"].replace("'", "").replace(",", "."))
        except ValueError:
            result["betrag"] = None

    # IBAN-Format prüfen (CH/LI = 21 Zeichen)
    iban = result["iban"]
    if not (iban.startswith(("CH", "LI")) and len(iban) == 21):
        log.warning("IBAN ungewöhnlich: %s", iban)

    return result


def _normalize_date(raw: str) -> str | None:
    """Datum in ISO-Format (YYYY-MM-DD) normalisieren."""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def extract_qr_from_pdf(pdf_path: str) -> list[dict]:
    """
    QR-Codes aus PDF extrahieren.
    Gibt Liste aller gefundenen Swiss QR Bills zurück.
    """
    results = []

    try:
        from pdf2image import convert_from_path
        from pyzbar.pyzbar import decode as pyzbar_decode
        from PIL import Image
    except ImportError as e:
        log.error("Abhängigkeit fehlt: %s — pip install pyzbar pdf2image pillow", e)
        return results

    # Seiten-Limit: QR-Rechnungen sind immer auf Seite 1-3
    # Mehr Seiten scannen ist verschwenderisch und RAM-gefährlich
    MAX_PAGES = int(os.environ.get("QR_MAX_PAGES", "5"))
    MAX_QR_CODES = int(os.environ.get("QR_MAX_CODES", "3"))

    try:
        # PDF in Bilder konvertieren (DPI 200 reicht für QR-Codes)
        # first_page/last_page verhindert RAM-Explosion bei grossen PDFs
        images = convert_from_path(pdf_path, dpi=200,
                                   first_page=1, last_page=MAX_PAGES)
        log.info("QR-Scan: %d Seiten (max %d) in '%s'",
                 len(images), MAX_PAGES, Path(pdf_path).name)
    except Exception as e:
        log.warning("PDF→Image Konvertierung fehlgeschlagen: %s", e)
        return results

    for page_num, image in enumerate(images, start=1):
        if len(results) >= MAX_QR_CODES:
            log.info("QR-Scan: Max QR-Codes (%d) erreicht — stoppe", MAX_QR_CODES)
            break
        try:
            # pyzbar via subprocess isoliert — segfault killt nur den Child,
            # nicht den ganzen pre_consume_qr Prozess
            import subprocess, base64, tempfile
            from PIL import Image as _PilImage
            # Bild in temp-File schreiben
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tmp_img_path = tf.name
            image.save(tmp_img_path, format="PNG")
            # pyzbar in separatem Prozess ausführen
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import sys,json; from pyzbar.pyzbar import decode; "
                 "from PIL import Image; "
                 f"img=Image.open({tmp_img_path!r}); "
                 "codes=[{'data':b.data.decode('utf-8','replace'),'type':b.type} for b in decode(img)]; "
                 "print(json.dumps(codes))"],
                capture_output=True, text=True, timeout=15
            )
            Path(tmp_img_path).unlink(missing_ok=True)
            if proc.returncode != 0:
                log.debug("pyzbar subprocess exit %d: %s", proc.returncode, proc.stderr[:100])
                continue
            import json as _json
            raw_codes = _json.loads(proc.stdout.strip() or "[]")
            barcodes = [type("BC", (), {"type": c["type"], "data": c["data"].encode()})() for c in raw_codes]
            for barcode in barcodes:
                if barcode.type not in ("QRCODE", "QR"):
                    continue
                try:
                    qr_text = barcode.data.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        qr_text = barcode.data.decode("latin-1")
                    except Exception:
                        continue

                parsed = parse_swiss_qr_bill(qr_text)
                if parsed:
                    parsed["seite"] = page_num
                    results.append(parsed)
                    log.info(
                        "Swiss QR Bill gefunden auf Seite %d: "
                        "Betrag=%s %s, Referenz=%s, Kreditor=%s",
                        page_num,
                        parsed.get("betrag"),
                        parsed.get("waehrung"),
                        parsed.get("referenz"),
                        parsed.get("kreditor_name"),
                    )
                else:
                    # Nicht-Swiss-QR-Code — loggen aber ignorieren
                    log.debug("QR-Code auf Seite %d ist kein Swiss QR Bill", page_num)
        except Exception as e:
            log.warning("QR-Scan Seite %d fehlgeschlagen: %s", page_num, e)

    return results


def main():
    pdf_path = os.environ.get("DOCUMENT_SOURCE_PATH", "")
    if not pdf_path:
        # Fallback: Pfad als Argument
        if len(sys.argv) > 1:
            pdf_path = sys.argv[1]
        else:
            log.error("DOCUMENT_SOURCE_PATH nicht gesetzt und kein Argument")
            sys.exit(0)  # Kein Fehler — pre_consume soll weiterlaufen

    if not Path(pdf_path).exists():
        log.warning("Datei nicht gefunden: %s", pdf_path)
        sys.exit(0)

    log.info("QR-Scan startet: %s", pdf_path)

    qr_bills = extract_qr_from_pdf(pdf_path)

    # Sidecar-Datei schreiben
    sidecar_path = Path(pdf_path).with_suffix("").as_posix() + "_qr_meta.json"

    if qr_bills:
        # Ersten (meist einzigen) QR-Bill verwenden
        # Bei mehreren: den mit dem höchsten Betrag wählen
        primary = max(qr_bills, key=lambda x: x.get("betrag") or 0)
        primary["alle_qr_bills"] = qr_bills if len(qr_bills) > 1 else None

        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(primary, f, ensure_ascii=False, indent=2)
        log.info("QR-Meta gespeichert: %s", sidecar_path)
    else:
        log.info("Kein Swiss QR Bill gefunden — kein Sidecar")
        # Leere Datei schreiben damit post_consume weiss dass gesucht wurde
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump({"source": "no_qr_found"}, f)

    sys.exit(0)


if __name__ == "__main__":
    main()
