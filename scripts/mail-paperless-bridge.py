#!/usr/bin/env python3
"""
mail-paperless-bridge — IMAP-Ordner → Paperless consume/

Pro E-Mail: ein kombiniertes PDF (Body + echte Anhänge, keine Inline-CID-Logos).
Mehrere Mails = mehrere PDF-Dateien in consume/ (kein Legacy-QR, kein Batch-Split nötig).

Typischer Ablauf:
  1. Mails in IMAP-Ordner legen (z. B. «Paperless» — manuell oder Mail-Helper-Regel)
  2. Cron/systemd startet dieses Skript auf CT 121
  3. PDFs landen atomisch in PAPERLESS_CONSUME_DIR → volle Pipeline

Aufruf:
  /opt/paperless-scripts/venv/bin/python3 mail-paperless-bridge.py
  mail-paperless-bridge.py --dry-run --limit 3
  mail-paperless-bridge.py --env /opt/paperless-scripts/mail-paperless-bridge.env

Abhängigkeiten (im paperless-scripts venv):
  pip install pypdf reportlab pillow

Optional: Mail-Helper-Regel «nach Ordner Paperless verschieben» statt manuellem Sortieren.
"""
from __future__ import annotations

import argparse
import email
import imaplib
import json
import logging
import os
import re
import sys
import textwrap
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("mail_paperless_bridge")

# PDF-Magic zum Erkennen vorhandener PDF-Anhänge
PDF_MAGIC = b"%PDF"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp"}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return "\n".join(self._parts)


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip("'\"")
        os.environ.setdefault(key, val)


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[-\s]+", "-", text).strip("-").lower()
    return (text[:max_len] or "mail").strip("-")


def decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def atomic_write_pdf(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    part.write_bytes(data)
    part.rename(target)
    log.info("→ consume: %s (%d KB)", target.name, len(data) // 1024)


@dataclass
class BridgeConfig:
    imap_host: str
    imap_user: str
    imap_password: str
    imap_folder: str = "Paperless"
    imap_export_folder: str = "Paperless/Exportiert"
    imap_port: int = 993
    imap_ssl: bool = True
    consume_dir: Path = Path("/mnt/paperless-data/consume")
    state_file: Path = Path("/opt/paperless-scripts/state/mail-paperless-bridge.json")
    skip_inline: bool = True
    mark_seen_on_success: bool = True
    delete_after_export: bool = False

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        def req(name: str) -> str:
            val = os.environ.get(name, "").strip()
            if not val:
                raise SystemExit(f"FEHLER: Umgebungsvariable {name} fehlt")
            return val

        return cls(
            imap_host=req("MAIL_BRIDGE_IMAP_HOST"),
            imap_user=req("MAIL_BRIDGE_IMAP_USER"),
            imap_password=req("MAIL_BRIDGE_IMAP_PASSWORD"),
            imap_folder=os.environ.get("MAIL_BRIDGE_IMAP_FOLDER", "Paperless"),
            imap_export_folder=os.environ.get("MAIL_BRIDGE_IMAP_EXPORT_FOLDER", "Paperless/Exportiert"),
            imap_port=int(os.environ.get("MAIL_BRIDGE_IMAP_PORT", "993")),
            imap_ssl=os.environ.get("MAIL_BRIDGE_IMAP_SSL", "true").lower() in ("1", "true", "yes"),
            consume_dir=Path(os.environ.get("MAIL_BRIDGE_CONSUME_DIR", "/mnt/paperless-data/consume")),
            state_file=Path(os.environ.get("MAIL_BRIDGE_STATE_FILE", "/opt/paperless-scripts/state/mail-paperless-bridge.json")),
            skip_inline=os.environ.get("MAIL_BRIDGE_SKIP_INLINE", "true").lower() in ("1", "true", "yes"),
            mark_seen_on_success=os.environ.get("MAIL_BRIDGE_MARK_SEEN", "true").lower() in ("1", "true", "yes"),
            delete_after_export=os.environ.get("MAIL_BRIDGE_DELETE_AFTER", "false").lower() in ("1", "true", "yes"),
        )


@dataclass
class MailPart:
    filename: str
    content_type: str
    payload: bytes
    is_inline: bool


@dataclass
class ParsedMail:
    uid: str
    subject: str
    sender: str
    date: Optional[datetime]
    body_text: str
    parts: list[MailPart] = field(default_factory=list)


def extract_body_text(msg: Message) -> str:
    plain: list[str] = []
    html: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() not in (None, "inline", "attachment"):
                continue
            ctype = part.get_content_type()
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain.append(text)
            elif ctype == "text/html":
                parser = _HTMLTextExtractor()
                parser.feed(text)
                html.append(parser.text())
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                parser = _HTMLTextExtractor()
                parser.feed(text)
                html.append(parser.text())
            else:
                plain.append(text)
        except Exception:
            pass

    body = "\n\n".join(plain).strip() or "\n\n".join(html).strip()
    return body or "(kein Textinhalt)"


def collect_parts(msg: Message, skip_inline: bool) -> list[MailPart]:
    parts: list[MailPart] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        disp = (part.get_content_disposition() or "").lower()
        is_inline = disp == "inline" or (disp != "attachment" and part.get("Content-ID"))
        if skip_inline and is_inline and not filename:
            continue
        if not filename and disp != "attachment":
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            continue
        if not payload:
            continue
        fname = decode_mime_header(filename) if filename else f"anhang-{len(parts)+1}.bin"
        parts.append(
            MailPart(
                filename=fname,
                content_type=part.get_content_type() or "application/octet-stream",
                payload=payload,
                is_inline=is_inline,
            )
        )
    return parts


def parse_message(uid: str, raw: bytes, skip_inline: bool) -> ParsedMail:
    msg = email.message_from_bytes(raw)
    subject = decode_mime_header(msg.get("Subject")) or "(ohne Betreff)"
    sender = decode_mime_header(msg.get("From")) or "unbekannt"
    date_hdr = msg.get("Date")
    try:
        date = parsedate_to_datetime(date_hdr) if date_hdr else None
    except Exception:
        date = None
    return ParsedMail(
        uid=uid,
        subject=subject,
        sender=sender,
        date=date,
        body_text=extract_body_text(msg),
        parts=collect_parts(msg, skip_inline),
    )


def render_body_pdf(mail: ParsedMail) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    x, y = 20 * mm, height - 25 * mm
    line_h = 5 * mm

    def writeln(text: str, bold: bool = False) -> None:
        nonlocal y
        if y < 25 * mm:
            c.showPage()
            y = height - 25 * mm
        if bold:
            c.setFont("Helvetica-Bold", 10)
        else:
            c.setFont("Helvetica", 9)
        for line in textwrap.wrap(text, width=95) or [""]:
            if y < 25 * mm:
                c.showPage()
                y = height - 25 * mm
            c.drawString(x, y, line[:500])
            y -= line_h

    writeln("E-Mail", bold=True)
    writeln(f"Betreff: {mail.subject}")
    writeln(f"Von: {mail.sender}")
    if mail.date:
        writeln(f"Datum: {mail.date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    writeln(f"IMAP-UID: {mail.uid}")
    y -= line_h
    writeln("Inhalt", bold=True)
    for para in mail.body_text.split("\n"):
        writeln(para)

    c.save()
    return buf.getvalue()


def attachment_to_pdf_pages(part: MailPart) -> list[bytes]:
    """Gibt eine Liste von PDF-Byte-Blobs zurück (ein Eintrag = ein PDF-Dokument zum Mergen)."""
    name_lower = part.filename.lower()
    ext = Path(name_lower).suffix

    if part.payload.startswith(PDF_MAGIC) or ext == ".pdf" or part.content_type == "application/pdf":
        return [part.payload]

    if ext in IMAGE_EXTS or part.content_type.startswith("image/"):
        from PIL import Image

        img = Image.open(BytesIO(part.payload))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        out = BytesIO()
        img.save(out, format="PDF")
        return [out.getvalue()]

    log.warning("Überspringe nicht-PDF/Bild-Anhang: %s (%s)", part.filename, part.content_type)
    return []


def merge_pdfs(chunks: Iterable[bytes]) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for blob in chunks:
        reader = PdfReader(BytesIO(blob))
        for page in reader.pages:
            writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def build_mail_pdf(mail: ParsedMail) -> bytes:
    chunks: list[bytes] = [render_body_pdf(mail)]
    for part in mail.parts:
        chunks.extend(attachment_to_pdf_pages(part))
    if len(chunks) == 1:
        return chunks[0]
    return merge_pdfs(chunks)


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("State-Datei defekt, starte neu: %s", path)
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def connect_imap(cfg: BridgeConfig) -> imaplib.IMAP4_SSL | imaplib.IMAP4:
    if cfg.imap_ssl:
        conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    else:
        conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
    conn.login(cfg.imap_user, cfg.imap_password)
    return conn


def ensure_folder(conn: imaplib.IMAP4, folder: str) -> None:
    try:
        conn.create(folder)
        log.info("IMAP-Ordner angelegt: %s", folder)
    except imaplib.IMAP4.error:
        pass


def get_uidvalidity(conn: imaplib.IMAP4, folder: str) -> str:
    """Echte UIDVALIDITY per STATUS — nicht SELECT-Rückgabe (das ist nur Message-Count)."""
    quoted = f'"{folder}"' if " " in folder else folder
    typ, data = conn.status(quoted, "(UIDVALIDITY)")
    if typ != "OK" or not data or not data[0]:
        return ""
    raw = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
    match = re.search(r"UIDVALIDITY\s+(\d+)", raw)
    return match.group(1) if match else ""


def process_mailbox(
    cfg: BridgeConfig,
    *,
    dry_run: bool = False,
    limit: int = 0,
) -> int:
    state = load_state(cfg.state_file)
    account_key = f"{cfg.imap_user}@{cfg.imap_host}:{cfg.imap_folder}"
    entry = state.setdefault(account_key, {})
    processed_uids: set[str] = set(entry.get("processed_uids", []))

    conn = connect_imap(cfg)
    try:
        typ, data = conn.select(f'"{cfg.imap_folder}"', readonly=False)
        if typ != "OK":
            raise SystemExit(f"IMAP SELECT fehlgeschlagen für Ordner: {cfg.imap_folder}")

        uidvalidity = get_uidvalidity(conn, cfg.imap_folder)
        if entry.get("uidvalidity") and uidvalidity and entry.get("uidvalidity") != uidvalidity:
            log.warning("UIDVALIDITY geändert — verarbeitete UIDs zurücksetzen")
            processed_uids.clear()
        if uidvalidity:
            entry["uidvalidity"] = uidvalidity

        typ, msgnums = conn.uid("SEARCH", None, "ALL")
        if typ != "OK" or not msgnums or not msgnums[0]:
            log.info("Keine Mails in %s", cfg.imap_folder)
            entry["processed_uids"] = sorted(processed_uids, key=int)
            save_state(cfg.state_file, state)
            return 0

        uids = msgnums[0].decode().split()
        pending = [u for u in uids if u not in processed_uids]
        if limit > 0:
            pending = pending[:limit]

        log.info("%d Mail(s) zu verarbeiten (Ordner %s)", len(pending), cfg.imap_folder)
        exported = 0
        deleted_any = False

        for uid in pending:
            typ, fetched = conn.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not fetched or not fetched[0]:
                log.error("FETCH fehlgeschlagen UID %s", uid)
                continue
            raw = fetched[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                log.error("Ungültige FETCH-Daten UID %s", uid)
                continue

            mail = parse_message(uid, raw, cfg.skip_inline)
            date_part = (
                mail.date.astimezone(timezone.utc).strftime("%Y%m%d")
                if mail.date
                else datetime.now(timezone.utc).strftime("%Y%m%d")
            )
            fname = f"mail_{date_part}_uid{uid}_{slugify(mail.subject)}.pdf"
            target = cfg.consume_dir / fname

            log.info(
                "Mail UID %s: «%s» — %d Anhang/Anhänge",
                uid,
                mail.subject[:80],
                len(mail.parts),
            )

            if dry_run:
                log.info("[dry-run] würde schreiben: %s", target)
                exported += 1
                continue

            try:
                pdf_bytes = build_mail_pdf(mail)
            except Exception as e:
                log.error("PDF-Build fehlgeschlagen UID %s: %s", uid, e)
                continue

            atomic_write_pdf(target, pdf_bytes)

            if cfg.imap_export_folder:
                ensure_folder(conn, cfg.imap_export_folder)
                conn.uid("COPY", uid, f'"{cfg.imap_export_folder}"')
                conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                deleted_any = True
            elif cfg.delete_after_export:
                conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                deleted_any = True
            elif cfg.mark_seen_on_success:
                conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")

            processed_uids.add(uid)
            entry["processed_uids"] = sorted(processed_uids, key=int)
            entry["last_export"] = datetime.now(timezone.utc).isoformat()
            save_state(cfg.state_file, state)
            exported += 1

        if deleted_any and not dry_run:
            conn.expunge()

        return exported
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="IMAP → Paperless consume/ Bridge")
    parser.add_argument("--env", type=Path, help="Pfad zu .env (KEY=VALUE)")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts schreiben")
    parser.add_argument("--limit", type=int, default=0, help="Max. Anzahl Mails pro Lauf")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    env_path = args.env or Path(os.environ.get("MAIL_BRIDGE_ENV", "/opt/paperless-scripts/mail-paperless-bridge.env"))
    load_dotenv(env_path)

    try:
        import pypdf  # noqa: F401
        import reportlab  # noqa: F401
    except ImportError as e:
        log.error(
            "Fehlende Python-Pakete: %s — im venv: pip install pypdf reportlab pillow",
            e,
        )
        return 1

    cfg = BridgeConfig.from_env()
    if not cfg.consume_dir.is_dir() and not args.dry_run:
        raise SystemExit(f"consume-Verzeichnis fehlt: {cfg.consume_dir}")

    count = process_mailbox(cfg, dry_run=args.dry_run, limit=args.limit)
    log.info("Fertig: %d Mail(s) exportiert", count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
