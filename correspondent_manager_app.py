"""
correspondent_manager/main.py
──────────────────────────────
FastAPI Review-UI für Korrespondenten-Kanonisierung.

Endpunkte:
  GET  /                    → Dashboard (Pending-Queue Übersicht)
  GET  /review/{index}      → Einzelner Pending-Eintrag zur Bearbeitung
  POST /review/{index}      → Freigabe / Ablehnung eines Eintrags
  GET  /correspondents      → Übersicht der Kanonisierungs-Map
  GET  /health              → Health-Check

Starten:
  uvicorn main:app --host 0.0.0.0 --port 8100 --reload

Nginx-Reverse-Proxy + Authentik Forward Auth davor schalten.
"""

import json
import os
import re
import asyncio
import functools
import fcntl
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__version__ = "2.55"  # 2.55: Fahrzeug-Tag-Dropdown, UI-Kontrast, Synonym-Warnung
UI_VERSION = "3.06"

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from brillenpass_parser import (
    PARSER_FORMAT,
    PARSER_LABELS,
    PARSER_NAMES,
    PARSER_VENDOR,
    VENDOR_LABELS,
    VENDOR_PARSERS,
    apply_brillenpass_doc_patches,
    build_version_id,
    chronological_prev_version,
    collect_document_ids,
    compute_brillenpass_diff,
    corr_supports_brillenpass,
    dedupe_brillenpass_versions_by_document,
    detect_parser,
    find_brillenpass_period_duplicate,
    has_brillenpass_values,
    hydrate_messung_from_diagnose,
    latest_brillenpass_version,
    merge_brillenpass_version,
    normalize_parser_name,
    parse_by_parser,
    resolve_brillenpass_aktuell,
    sort_brillenpass_versions,
    normalize_gueltig_ab_iso,
    vendor_from_parser,
)

# ──────────────────────────────────────────────
# Konfiguration
# ──────────────────────────────────────────────
PAPERLESS_API_URL   = os.environ.get("PAPERLESS_API_URL", "http://localhost:8000/api")
PAPERLESS_API_TOKEN = os.environ.get("PAPERLESS_TOKEN", "") or os.environ.get("PAPERLESS_API_TOKEN", "")
CORRESPONDENTS_JSON = os.environ.get("CORRESPONDENTS_JSON", "data/correspondents.json")
PENDING_JSONL       = os.environ.get("PENDING_JSONL", "data/pending_correspondents.jsonl")
TAGS_JSON           = Path(os.environ.get("TAGS_JSON", "/opt/paperless-scripts/training/tags.json"))
FAMILY_JSON         = Path(os.environ.get("FAMILY_JSON", "/opt/paperless-scripts/training/family.json"))
BRILLENPAESSE_JSON  = Path(os.environ.get("BRILLENPAESSE_JSON", "/opt/paperless-scripts/training/brillenpaesse.json"))
PENDING_BRILLENPASS_JSONL = Path(os.environ.get(
    "PENDING_BRILLENPASS_JSONL", "/opt/paperless-scripts/training/pending_brillenpass.jsonl",
))
PAPERLESS_VIEW_GROUPS   = [g.strip() for g in os.environ.get("PAPERLESS_VIEW_GROUPS", "family,Eltern").split(",")]
PAPERLESS_CHANGE_GROUPS = [g.strip() for g in os.environ.get("PAPERLESS_CHANGE_GROUPS", "Eltern").split(",")]
PENDING_REVIEW_TAG       = os.environ.get("PENDING_REVIEW_TAG",       "pending_review")
PENDING_QS_TAG           = os.environ.get("PENDING_QS_TAG",           "pending_qs")
PENDING_NEW_CORR_TAG     = os.environ.get("PENDING_NEW_CORR_TAG",      "pending_new_correspondent")
PENDING_BRILLENPASS_TAG  = os.environ.get("PENDING_BRILLENPASS_TAG",  "pending_brillenpass")
BRILLENPASS_DEDUP_DAYS     = int(os.environ.get("BRILLENPASS_DEDUP_DAYS", "21"))
PAPERLESS_CONSUME_DIR      = os.environ.get("PAPERLESS_CONSUME_DIR", "/mnt/paperless-data/consume")
PAPERLESS_MEDIA_ROOT       = os.environ.get("PAPERLESS_MEDIA_ROOT", "/mnt/paperless-media")
LEGACY_SPLIT_TMP           = Path(os.environ.get("LEGACY_SPLIT_TMP", "/tmp/legacy-qr-split"))
from legacy_split_by_qr import DEFAULT_QR_REGEX as _LEGACY_QR_DEFAULT, normalize_legacy_qr_regex
LEGACY_SPLIT_QR_REGEX      = normalize_legacy_qr_regex(
    os.environ.get("LEGACY_SPLIT_QR_REGEX", _LEGACY_QR_DEFAULT),
)
ALL_PENDING_TAGS         = {PENDING_REVIEW_TAG, PENDING_QS_TAG, PENDING_NEW_CORR_TAG, PENDING_BRILLENPASS_TAG}

PAPERLESS_HEADERS = {
    "Authorization": f"Token {PAPERLESS_API_TOKEN}",
    "Content-Type": "application/json",
}
PAPERLESS_OWNER_ID     = int(os.environ.get("PAPERLESS_OWNER_ID", "3"))  # deprecated — wird nicht verwendet, siehe _default_permissions()
_PERM_VIEW_GROUP_IDS   = [int(g) for g in os.environ.get("PAPERLESS_VIEW_GROUP_IDS",  "1,2").split(",") if g.strip().isdigit()]
_PERM_CHANGE_GROUP_IDS = [int(g) for g in os.environ.get("PAPERLESS_CHANGE_GROUP_IDS", "2").split(",") if g.strip().isdigit()]


def _default_permissions() -> dict:
    """
    Zentrale Permissions — EINZIGE Stelle für Berechtigungen.

    REGEL (nie ändern):
      owner = null (kein Owner — sonst sehen andere Benutzer das Objekt nicht)
      view  = family + Eltern (IDs aus PAPERLESS_VIEW_GROUP_IDS)
      change = Eltern (IDs aus PAPERLESS_CHANGE_GROUP_IDS)

    PAPERLESS_OWNER_ID in .env wird bewusst NICHT verwendet:
      Owner-Konzept ist für Mehrbenutzer-Haushalt ungeeignet.
      Gruppen-Permissions reichen vollständig.
    """
    return {
        "set_permissions": {
            "view":   {"users": [], "groups": _PERM_VIEW_GROUP_IDS},
            "change": {"users": [], "groups": _PERM_CHANGE_GROUP_IDS},
        },
    }

log = logging.getLogger("correspondent_manager")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="paper.manager", version="2.0.0")

# Lange Jobs (Brillenpass Vision ~120s) — eigener Pool, nicht Starlette-Default
_BG_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pm-bg")
# Session-Prüfung — eigener Pool (nicht asyncio.to_thread / Default-Pool)
_AUTH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pm-auth")
# Legacy QR-Split — pro Seite 600 DPI, kann Minuten dauern
_LEGACY_SPLIT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pm-lsplit")
_LEGACY_SPLIT_JOBS: dict[str, dict] = {}
_LEGACY_SPLIT_JOBS_LOCK = threading.Lock()

# Session-Cache — reduziert Paperless-Profile-Calls bei init()-API-Burst
_SESSION_CACHE: dict[str, tuple[bool, float]] = {}
_SESSION_CACHE_TTL = 60.0
_session_cache_lock = threading.Lock()


def _validate_paperless_session(
    session_cookie: str,
    csrf: str,
    paperless_base: str,
    paperless_internal: str,
) -> bool:
    """Paperless-Session prüfen — nur in _AUTH_EXECUTOR aufrufen."""
    if not session_cookie:
        return False
    cache_key = f"{session_cookie}:{csrf}"
    now = time.monotonic()
    with _session_cache_lock:
        cached = _SESSION_CACHE.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]
    cookie_hdr = f"sessionid={session_cookie}"
    if csrf:
        cookie_hdr += f"; csrftoken={csrf}"
    headers = {"Cookie": cookie_hdr}
    bases = [b for b in (paperless_base, paperless_internal) if b]
    valid = False
    for attempt in range(2):
        for base in bases:
            try:
                r = requests.get(
                    f"{base.rstrip('/')}/api/profile/",
                    headers=headers,
                    timeout=(3, 8),
                )
                if r.status_code == 200:
                    valid = True
                    break
            except Exception:
                continue
        if valid:
            break
        if attempt == 0:
            time.sleep(0.3)
    if valid:
        with _session_cache_lock:
            _SESSION_CACHE[cache_key] = (True, time.monotonic() + _SESSION_CACHE_TTL)
    return valid


async def _session_valid_for_request(request: Request, paperless_internal: str) -> bool:
    session_cookie = request.cookies.get("sessionid")
    if not session_cookie:
        return False
    csrf = request.cookies.get("csrftoken") or ""
    paperless_base = _effective_paperless_url(request)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _AUTH_EXECUTOR,
        functools.partial(
            _validate_paperless_session,
            session_cookie,
            csrf,
            paperless_base,
            paperless_internal,
        ),
    )


def _effective_paperless_url(request: Request | None = None) -> str:
    """Request-host-aware Paperless-URL für Links und Login-Redirect."""
    canonical = os.environ.get("PAPERLESS_URL", "http://localhost:8000")
    if request is None:
        return canonical
    host = request.headers.get("host", "localhost:8100")
    proto = request.headers.get("x-forwarded-proto", "http")
    host_without_port = host.split(":")[0]
    if host_without_port.replace(".", "").isdigit():
        return f"http://{host_without_port}:8000"
    if host_without_port in ("localhost", "127.0.0.1"):
        return canonical
    return f"{proto}://{host_without_port}"


def _parse_geburtsdatum(geb: str) -> tuple[int, int, int] | None:
    """Parst Geburtsdatum: 15.5.1980, 15.05.1980, 15.5.80, 1980-05-15."""
    import re as _re
    geb = (geb or "").strip()
    if not geb:
        return None
    m = _re.match(r"^(\d{4})-(\d{2})-(\d{2})$", geb)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return d, mo, y
        return None
    m = _re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$", geb)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y = 2000 + y if y < 30 else 1900 + y
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return d, mo, y
    return None


def _normalize_geburtsdatum(geb: str) -> str:
    parsed = _parse_geburtsdatum(geb)
    if not parsed:
        return geb.strip()
    d, mo, y = parsed
    return f"{d}.{mo}.{y}"


@app.middleware("http")
async def require_paperless_session(request: Request, call_next):
    """Prüft ob eine gültige Paperless-Session vorhanden ist.
    API-Calls (/api/*): Token-Check via PAPER_MANAGER_TOKEN falls gesetzt,
    sonst Session-Cookie prüfen (gegen request-host-aware Paperless-URL).
    Browser-Requests: kein Cookie → immer Redirect zu Paperless Login.
    """
    path = request.url.path
    paperless_login_base = _effective_paperless_url(request)
    paperless_internal = os.environ.get("PAPERLESS_INTERNAL_URL",
                         os.environ.get("PAPERLESS_URL", "http://localhost:8000"))

    # Health + HTML-Shell ohne Auth — APIs bleiben geschützt
    if path == "/health" or path in ("/", ""):
        return await call_next(request)

    # Proxy-Endpoints: kein Auth nötig — Backend holt Daten selbst mit Token
    if path.startswith("/api/proxy/"):
        return await call_next(request)

    # API-Calls: Token oder Session prüfen
    if path.startswith("/api/"):
        if _INTERNAL_TOKEN:
            token = (
                request.headers.get("X-Paper-Manager-Token") or
                request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            )
            if token == _INTERNAL_TOKEN:
                return await call_next(request)
        if await _session_valid_for_request(request, paperless_internal):
            return await call_next(request)
        from fastapi.responses import JSONResponse as _JR
        return _JR(status_code=401, content={"detail": "Nicht authentifiziert"})

    # Browser-Requests (z. B. /corr-manager/): Session oder Login-Redirect
    if await _session_valid_for_request(request, paperless_internal):
        return await call_next(request)

    host = request.headers.get("host", "localhost:8100")
    next_url = f"http://{host}/"
    login_url = f"{paperless_login_base}/accounts/login/?next={next_url}"
    return RedirectResponse(url=login_url)

# Interner API-Token — verhindert direkten Zugriff falls nginx/Authentik ausfällt
# Gesetzt via PAPER_MANAGER_TOKEN in .env (leer = kein Schutz, nur intern)
_INTERNAL_TOKEN = os.environ.get("PAPER_MANAGER_TOKEN", "")


def _check_internal_token(request):
    """Legacy — Token-Check ist jetzt in der Middleware.
    Bleibt für Rückwärtskompatibilität, tut aber nichts mehr."""
    pass


# ══════════════════════════════════════════════
# PYDANTIC MODELS
# ══════════════════════════════════════════════

class ExtraktionsMuster(BaseModel):
    """Extraktions-Muster für ein Custom Field eines Korrespondenten."""
    regex:            Optional[str]       = None   # Regex-Pattern
    label_hints:      Optional[list[str]] = None   # Suchbegriffe für Vision ("Police Nr.", ...)
    beispiel:         Optional[str]       = None   # Beispielwert für Regex-Assistent
    custom_field_id:  Optional[int]       = None   # Paperless Custom Field ID


class ReviewDecision(BaseModel):
    action: str             # "approve" | "reject" | "approve_merge" | "approve_ergaenzung"
    canonical_name:          Optional[str]       = None
    varianten:               Optional[list[str]] = None
    match_strings:           Optional[list[str]] = None
    typ:                     Optional[str]       = None   # Korrespondenten-Typ (legacy)
    default_dokumenttyp:     Optional[str]       = None   # Standard-Dokumenttyp (Name)
    default_dokumenttyp_id:  Optional[int]       = None   # Paperless DocumentType ID
    typische_ordner:         Optional[list[str]] = None
    notiz:                   Optional[str]       = None
    kuerzel:                 Optional[str]       = None
    merge_ziel_name:         Optional[str]       = None
    assign_to_existing_name: Optional[str]       = None
    reviewed_by:             Optional[str]       = "admin"
    extraktion_muster:       Optional[dict]      = None   # {feldname: ExtraktionsMuster}
    erwartungen:             Optional[dict]      = None   # {hat_qr_rechnung: bool, ...}
    identifikatoren:         Optional[dict]      = None   # {uid:[], iban:[], email:[], telefon:[]}


# ══════════════════════════════════════════════
# FILE HELPERS
# ══════════════════════════════════════════════

def load_pending() -> list[dict]:
    path = Path(PENDING_JSONL)
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


# Lock-File für pending JSONL — verhindert gleichzeitigen Zugriff von UI + post_consume
PENDING_LOCK_PATH = Path("/tmp/paperless_pending.lock")


@contextmanager
def pending_write_lock():
    """Exklusiver Lock für pending_correspondents.jsonl — auch gegen post_consume."""
    PENDING_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(PENDING_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def save_pending(entries: list[dict]):
    """Atomar schreiben unter Lock — safe gegen gleichzeitigen post_consume Zugriff."""
    import tempfile as _tmp
    path = Path(PENDING_JSONL)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pending_write_lock():
        fd, tmp_path = _tmp.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp_path, path)
        except Exception:
            os.unlink(tmp_path)
            raise


def _load_manifest_pfade() -> set:
    """Alle bekannten Pfade aus manifest.json laden."""
    manifest_path = Path(os.environ.get("MANIFEST_PATH",
        "/opt/paperless-scripts/training/manifest.json"))
    if not manifest_path.exists():
        return set()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {e["pfad"] for e in data.get("ordner", []) if e.get("pfad")}
    except Exception:
        return set()


# Cache für Validation-Index — wird bei save_corr_map invalidiert
_VALIDATION_CACHE: dict = {}
_VALIDATION_CACHE_VERSION: int = 0


def _build_validation_index(corr_map: dict, exclude_name: str = None) -> dict:
    """O(n) Index für Validation — einmalig bauen, wiederverwenden.
    Verhindert O(n²) bei grossen Korrespondenten-Maps.
    """
    all_names:    set  = set()
    all_matches:  dict = {}
    all_varianten: dict = {}
    all_uids:     dict = {}
    all_ibans:    dict = {}
    all_emails:   dict = {}
    all_telefone: dict = {}
    for e in corr_map.get("eintraege", []):
        if e["name"] == (exclude_name or ""):
            continue
        all_names.add(e["name"].lower())
        for m in e.get("match", []):
            all_matches[m.lower()] = e["name"]
        for v in e.get("varianten", []):
            all_varianten[v.lower()] = e["name"]
        ident = e.get("identifikatoren") or {}
        for uid in ident.get("uid", []) or []:
            n = _norm_corr_uid(uid)
            if n:
                all_uids[n] = e["name"]
        for iban in ident.get("iban", []) or []:
            n = _norm_corr_iban(iban)
            if n:
                all_ibans[n] = e["name"]
        for em in ident.get("email", []) or []:
            n = _norm_corr_email(em)
            if n:
                all_emails[n] = e["name"]
        for tel in ident.get("telefon", []) or []:
            n = _norm_corr_telefon(tel)
            if n:
                all_telefone[n] = e["name"]
    return {
        "names":     all_names,
        "matches":   all_matches,
        "varianten": all_varianten,
        "uids":      all_uids,
        "ibans":     all_ibans,
        "emails":    all_emails,
        "telefone":  all_telefone,
    }


def _check_kuerzel_unique(kuerzel: str, exclude_name: str = "", corr_map: dict = None) -> bool:
    """Prüft ob kuerzel über alle Korrespondenten einmalig ist.
    exclude_name: eigenen Eintrag beim Edit ausschliessen.
    """
    if not kuerzel or not corr_map:
        return True
    k = kuerzel.strip().upper()
    for e in corr_map.get("eintraege", []):
        if e["name"] == exclude_name:
            continue
        if (e.get("kuerzel") or "").strip().upper() == k:
            return False
    return True


def _validate_correspondent_entry(
    name: str,
    match_strings: list,
    varianten: list,
    typische_ordner: list,
    corr_map: dict,
    exclude_name: str = None,
    identifikatoren: dict | None = None,
) -> tuple:
    """Validiert Eindeutigkeit und Konsistenz. O(n) dank Index.

    Checks:
      1. name unique global
      2. match-Strings global unique (Paperless matcht sonst falsch)
      3. varianten global unique (Warning)
      4. typische_ordner müssen in manifest.json existieren
      5. Duplikate innerhalb der Listen
    """
    errors   = []
    warnings = []

    idx = _build_validation_index(corr_map, exclude_name)
    manifest_pfade = _load_manifest_pfade()

    # 1. Name unique
    if name.lower() in idx["names"]:
        errors.append(f"Name '{name}' existiert bereits")

    # 2. Match-Strings global unique (hart)
    for m in match_strings:
        if m.lower() in idx["matches"]:
            errors.append(
                f"Match-String '{m}' bereits bei '{idx['matches'][m.lower()]}' "
                f"— Paperless würde falsch matchen"
            )

    # 3. Varianten global unique (weich)
    for v in varianten:
        if v.lower() in idx["varianten"]:
            warnings.append(
                f"Variante '{v}' bereits bei '{idx['varianten'][v.lower()]}'"
            )

    # 4. Typische Ordner im Manifest — unbekannte Ordner sind Warnings, keine Errors
    # (sie werden nach der Freigabe automatisch als pending im Manifest angelegt)
    if manifest_pfade:
        for o in typische_ordner:
            if o not in manifest_pfade:
                warnings.append(f"Ordner '{o}' noch nicht im Manifest — wird automatisch als pending angelegt")

    # 5. Duplikate innerhalb Listen
    for label, lst in [("varianten", varianten), ("match", match_strings),
                        ("typische_ordner", typische_ordner)]:
        seen: set = set()
        for item in lst:
            if item.lower() in seen:
                errors.append(f"Duplikat in {label}: '{item}'")
            seen.add(item.lower())

    ident = _normalize_identifikatoren(identifikatoren or {})
    for uid in ident.get("uid", []):
        n = _norm_corr_uid(uid)
        if n in idx["uids"]:
            errors.append(f"UID '{uid}' bereits bei '{idx['uids'][n]}'")
    for iban in ident.get("iban", []):
        n = _norm_corr_iban(iban)
        if n in idx["ibans"]:
            errors.append(f"IBAN '{iban}' bereits bei '{idx['ibans'][n]}'")
    for em in ident.get("email", []):
        n = _norm_corr_email(em)
        if n in idx["emails"]:
            errors.append(f"E-Mail '{em}' bereits bei '{idx['emails'][n]}'")
    for tel in ident.get("telefon", []):
        n = _norm_corr_telefon(tel)
        if n in idx["telefone"]:
            warnings.append(f"Telefon '{tel}' bereits bei '{idx['telefone'][n]}'")

    for label, lst in [("uid", ident.get("uid", [])), ("iban", ident.get("iban", [])),
                        ("email", ident.get("email", [])), ("telefon", ident.get("telefon", []))]:
        seen_id: set[str] = set()
        for item in lst:
            if label == "uid":
                norm = _norm_corr_uid(item)
            elif label == "iban":
                norm = _norm_corr_iban(item)
            elif label == "email":
                norm = _norm_corr_email(item)
            else:
                norm = _norm_corr_telefon(item)
            if norm in seen_id:
                errors.append(f"Duplikat in identifikatoren.{label}: '{item}'")
            seen_id.add(norm)

    return errors, warnings


def load_corr_map() -> dict:
    path = Path(CORRESPONDENTS_JSON)
    if not path.exists():
        return {"version": "1.0", "eintraege": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_string_list(raw) -> list[str]:
    """String-Liste trimmen, deduplizieren (Reihenfolge beibehalten)."""
    if not raw or not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _norm_corr_uid(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"[^A-Za-z0-9]", "", str(raw).upper())
    if s.startswith("CHE") and len(s) >= 12:
        return s[:12]
    return s


def _norm_corr_iban(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw).upper())


def _norm_corr_telefon(raw: str) -> str:
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("41") and len(digits) >= 11:
        return digits
    if digits.startswith("0") and len(digits) >= 10:
        return "41" + digits[1:]
    return digits


def _norm_corr_email(raw: str) -> str:
    return str(raw or "").strip().lower()


def _normalize_identifikatoren(raw) -> dict:
    """UID/IBAN/E-Mail/Telefon-Listen normalisieren (Anzeigeformat behalten)."""
    if not raw or not isinstance(raw, dict):
        return {"uid": [], "iban": [], "email": [], "telefon": []}
    uid_seen: set[str] = set()
    iban_seen: set[str] = set()
    email_seen: set[str] = set()
    tel_seen: set[str] = set()
    uids, ibans, emails, tels = [], [], [], []
    for item in raw.get("uid") or []:
        s = str(item).strip()
        n = _norm_corr_uid(s)
        if s and n and n not in uid_seen:
            uid_seen.add(n)
            uids.append(s)
    for item in raw.get("iban") or []:
        s = str(item).strip()
        n = _norm_corr_iban(s)
        if s and n and n not in iban_seen:
            iban_seen.add(n)
            ibans.append(s)
    for item in raw.get("email") or []:
        s = str(item).strip().lower()
        n = _norm_corr_email(s)
        if s and n and "@" in n and n not in email_seen:
            email_seen.add(n)
            emails.append(s)
    for item in raw.get("telefon") or []:
        s = str(item).strip()
        n = _norm_corr_telefon(s)
        if s and n and n not in tel_seen:
            tel_seen.add(n)
            tels.append(s)
    return {"uid": uids, "iban": ibans, "email": emails, "telefon": tels}


def _normalize_brillenpass(raw) -> dict:
    if not raw or not isinstance(raw, dict):
        return {"aktiv": False, "vendor": "", "parser": "", "parsers": [], "typische_begriffe": []}
    begriffe = _normalize_string_list(raw.get("typische_begriffe") or [])
    vendor = str(raw.get("vendor") or "").strip().lower()
    if vendor not in VENDOR_PARSERS:
        vendor = ""
    raw_parsers = raw.get("parsers") or []
    if isinstance(raw_parsers, str):
        raw_parsers = [raw_parsers]
    single = str(raw.get("parser") or "").strip()
    parsers: list[str] = []
    for p in list(raw_parsers) + ([single] if single else []):
        name = normalize_parser_name(str(p or "").strip())
        if name in PARSER_NAMES and name not in parsers:
            parsers.append(name)
    if not vendor and parsers:
        vendors = {vendor_from_parser(p) for p in parsers}
        vendors.discard(None)
        if len(vendors) == 1:
            vendor = vendors.pop()
    aktiv = bool(raw.get("aktiv")) and bool(vendor or parsers)
    return {
        "aktiv": aktiv,
        "vendor": vendor if aktiv else "",
        "parser": parsers[0] if parsers else "",
        "parsers": parsers if aktiv else [],
        "typische_begriffe": begriffe if aktiv else [],
    }


def _normalize_beziehung_fields(bez: dict) -> None:
    """stichworte[] normalisieren (klein, getrimmt, dedupliziert)."""
    if "stichworte" not in bez:
        return
    raw = bez.get("stichworte") or []
    if not isinstance(raw, list):
        bez["stichworte"] = []
        return
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        s = str(item).strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    bez["stichworte"] = out


def save_corr_map(data: dict):
    """Atomar schreiben via temp-file + rename — verhindert Race Condition mit post_consume."""
    import tempfile, os as _os
    path = Path(CORRESPONDENTS_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Temp-File im gleichen Verzeichnis → atomisches rename möglich
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(data, f, ensure_ascii=False, indent=2)
            fcntl.flock(f, fcntl.LOCK_UN)
        _os.replace(tmp_path, path)  # atomar
    except Exception:
        _os.unlink(tmp_path)
        raise


# ══════════════════════════════════════════════
# PAPERLESS API HELPERS
# ══════════════════════════════════════════════

def pl_get(path: str, params: dict = None) -> dict:
    url = f"{PAPERLESS_API_URL.rstrip('/')}/{path.lstrip('/')}"
    r = requests.get(url, headers=PAPERLESS_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def pl_post(path: str, data: dict) -> dict:
    url = f"{PAPERLESS_API_URL.rstrip('/')}/{path.lstrip('/')}"
    r = requests.post(url, headers=PAPERLESS_HEADERS, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def pl_patch(path: str, data: dict) -> dict:
    url = f"{PAPERLESS_API_URL.rstrip('/')}/{path.lstrip('/')}"
    r = requests.patch(url, headers=PAPERLESS_HEADERS, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def pl_delete(path: str) -> None:
    url = f"{PAPERLESS_API_URL.rstrip('/')}/{path.lstrip('/')}"
    r = requests.delete(url, headers=PAPERLESS_HEADERS, timeout=30)
    r.raise_for_status()


def pl_download_pdf(doc_id: int, *, original: bool = False) -> tuple[bytes, str]:
    """PDF-Bytes und Originaldateiname aus Paperless."""
    doc = pl_get(f"/documents/{doc_id}/")
    url = f"{PAPERLESS_API_URL.rstrip('/')}/documents/{doc_id}/download/"
    if original:
        url += "?original=true"
    r = requests.get(
        url,
        headers=PAPERLESS_HEADERS,
        timeout=120,
        stream=True,
    )
    r.raise_for_status()
    return r.content, doc.get("original_file_name") or f"{doc_id}.pdf"


def pl_download_pdf_variants(doc_id: int) -> list[tuple[str, bytes, str]]:
    """Original + Archiv-PDF laden (was verfügbar ist)."""
    out: list[tuple[str, bytes, str]] = []
    for original, label in ((True, "original"), (False, "archiv")):
        try:
            pdf_bytes, name = pl_download_pdf(doc_id, original=original)
            if pdf_bytes:
                out.append((label, pdf_bytes, name))
        except Exception as e:
            log.warning("Legacy-Split PDF %s für #%s: %s", label, doc_id, e)
    return out


def _legacy_split_progress(job_key: str | None, message: str) -> None:
    if job_key:
        _lsplit_job_set(job_key, status="running", message=message)
    log.info("Legacy-Split: %s", message)


def _legacy_split_materialize_pdf(
    doc_id: int,
    job_key: str | None = None,
) -> tuple[Path, Path, str, str] | None:
    """
    PDF nach lokalem /tmp kopieren — Ghostscript nie direkt auf NAS-Mount.
    Returns (local_pdf, work_dir, source_label, orig_filename).
    """
    work = LEGACY_SPLIT_TMP / str(doc_id)
    work.mkdir(parents=True, exist_ok=True)
    local_pdf = work / "source.pdf"
    orig_name = f"{doc_id}.pdf"

    _legacy_split_progress(job_key, f"Dok #{doc_id}: PDF von Paperless laden…")
    try:
        data, orig_name = pl_download_pdf(doc_id, original=True)
        if data:
            local_pdf.write_bytes(data)
            log.info("Legacy-Split #%s: API-Original → %s (%d bytes)", doc_id, local_pdf, len(data))
            return local_pdf, work, "original", orig_name
    except Exception as e:
        log.warning("Legacy-Split API-Original #%s: %s", doc_id, e)

    padded = f"{doc_id:07d}.pdf"
    for sub, label in (("originals", "original"), ("archive", "archiv")):
        src = Path(PAPERLESS_MEDIA_ROOT) / "documents" / sub / padded
        if src.is_file():
            _legacy_split_progress(job_key, f"Dok #{doc_id}: Kopie NAS → /tmp…")
            shutil.copy2(src, local_pdf)
            log.info("Legacy-Split #%s: %s → %s", doc_id, src, local_pdf)
            try:
                doc = pl_get(f"/documents/{doc_id}/")
                orig_name = doc.get("original_file_name") or orig_name
            except Exception as e:
                log.warning("Legacy-Split Metadaten #%s: %s", doc_id, e)
            return local_pdf, work, label, orig_name

    _legacy_split_progress(job_key, f"Dok #{doc_id}: Archiv-PDF laden…")
    try:
        data, orig_name = pl_download_pdf(doc_id, original=False)
        if data:
            local_pdf.write_bytes(data)
            log.info("Legacy-Split #%s: API-Archiv → %s (%d bytes)", doc_id, local_pdf, len(data))
            return local_pdf, work, "archiv", orig_name
    except Exception as e:
        log.warning("Legacy-Split API-Archiv #%s: %s", doc_id, e)

    return None


def _legacy_split_cleanup(work_dir: Path | None) -> None:
    if work_dir and work_dir.exists():
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception as e:
            log.warning("Legacy-Split cleanup %s: %s", work_dir, e)


def _legacy_split_resolve_document(
    doc_id: int,
    regex: str,
    job_key: str | None = None,
) -> tuple[dict, str, Path, Path] | None:
    """PDF in /tmp materialisieren und lokal scannen (wie CLI auf /opt/scan.pdf)."""
    from legacy_split_by_qr import (
        DEFAULT_DPI,
        _marker_score,
        scan_pdf_file,
    )

    materialized = _legacy_split_materialize_pdf(doc_id, job_key=job_key)
    if not materialized:
        return None

    local_pdf, work_dir, source, orig_name = materialized
    _legacy_split_progress(job_key, f"Dok #{doc_id}: QR scannen (lokal, Ghostscript ~10s)…")

    best = scan_pdf_file(
        str(local_pdf), source, regex=regex, dpi=DEFAULT_DPI, isolated=True,
    )
    if _marker_score(best["markers"]) >= 1:
        log.info(
            "Legacy-Split #%s: %d Marker in %s",
            doc_id, _marker_score(best["markers"]), local_pdf,
        )
        return best, orig_name, local_pdf, work_dir

    if source != "archiv":
        _legacy_split_progress(job_key, f"Dok #{doc_id}: Archiv-PDF laden & quick-scan…")
        try:
            arch_bytes, arch_name = pl_download_pdf(doc_id, original=False)
            arch_local = work_dir / "archiv.pdf"
            arch_local.write_bytes(arch_bytes)
            cand = scan_pdf_file(
                str(arch_local), "archiv", regex=regex, dpi=DEFAULT_DPI, quick=True, isolated=True,
            )
            if _marker_score(cand["markers"]) > _marker_score(best["markers"]):
                return cand, arch_name, arch_local, work_dir
        except Exception as e:
            log.warning("Legacy-Split Archiv-Fallback #%s: %s", doc_id, e)

    if _marker_score(best["markers"]) >= 1:
        return best, orig_name, local_pdf, work_dir

    return best, orig_name, local_pdf, work_dir


def _lsplit_job_key(doc_id: int, dry_run: bool) -> str:
    return f"{doc_id}:{'preview' if dry_run else 'split'}"


def _lsplit_job_get(key: str) -> dict:
    with _LEGACY_SPLIT_JOBS_LOCK:
        return dict(_LEGACY_SPLIT_JOBS.get(key) or {})


def _lsplit_job_set(key: str, **fields) -> None:
    with _LEGACY_SPLIT_JOBS_LOCK:
        job = dict(_LEGACY_SPLIT_JOBS.get(key) or {})
        job.update(fields)
        job["updated_at"] = time.time()
        _LEGACY_SPLIT_JOBS[key] = job


def _legacy_split_preview_payload(
    best: dict,
    orig_name: str,
    *,
    doc_id: int,
    regex: str,
    scan_seconds: float,
    dry_run: bool,
) -> dict:
    from legacy_split_by_qr import has_real_qr_splits

    markers = best["markers"]
    total = best["total"]
    qr_debug = best["qr_debug"]
    scan_meta = best.get("scan_meta") or {}
    source = best["label"]
    qr_matched = sum(1 for x in qr_debug if x.get("matched"))

    preview = []
    for i, (barcode, from_page) in enumerate(markers):
        to_page = markers[i + 1][1] - 1 if i + 1 < len(markers) else total
        preview.append({
            "barcode": barcode,
            "from_page": from_page,
            "to_page": to_page,
            "is_prefix": barcode == "Kein_Barcode",
        })

    if dry_run:
        if not has_real_qr_splits(markers):
            pages_with_qr = 0
            return {
                "ok": False,
                "dry_run": True,
                "document_id": doc_id,
                "pages": total,
                "splits": [],
                "qr_matched": qr_matched,
                "qr_seen": len(qr_debug),
                "qr_debug": qr_debug[:50],
                "source": source,
                "scan_seconds": scan_seconds,
                "scan_meta": scan_meta,
                "message": (
                    f"Keine QR-Codes passend zu Regex auf {total} Seiten "
                    f"(Quelle: {source}, {scan_seconds}s, Regex: {regex})"
                ),
            }
        pages_with_qr = len({d["page"] for d in qr_debug if d.get("matched")})
        render = scan_meta.get("backend", "?")
        dpi = scan_meta.get("dpi", "?")
        return {
            "ok": True,
            "dry_run": True,
            "document_id": doc_id,
            "pages": total,
            "splits": preview,
            "qr_matched": qr_matched,
            "qr_seen": len(qr_debug),
            "qr_debug": qr_debug[:50],
            "source": source,
            "scan_seconds": scan_seconds,
            "scan_meta": scan_meta,
            "orig_name": orig_name,
            "message": (
                f"{total} Seiten → {len(preview)} Teile "
                f"({qr_matched} QR auf {pages_with_qr} Seiten, "
                f"{source}, {render}@{dpi}dpi, {scan_seconds}s)"
            ),
        }
    return {
        "best": best,
        "orig_name": orig_name,
        "preview": preview,
        "source": source,
        "scan_seconds": scan_seconds,
        "scan_meta": scan_meta,
    }


def _run_legacy_split_job(
    doc_id: int,
    regex: str,
    dry_run: bool,
    consume_dir: str,
    job_key: str,
) -> None:
    from legacy_split_by_qr import has_real_qr_splits, split_pdf_at_markers

    t0 = time.monotonic()
    work_dir: Path | None = None
    try:
        resolved = _legacy_split_resolve_document(doc_id, regex, job_key=job_key)
        scan_seconds = round(time.monotonic() - t0, 1)
        if not resolved:
            _lsplit_job_set(
                job_key,
                status="error",
                error=f"Dokument #{doc_id} — PDF nicht ladbar",
                scan_seconds=scan_seconds,
            )
            return

        best, orig_name, local_pdf, work_dir = resolved

        if dry_run:
            payload = _legacy_split_preview_payload(
                best, orig_name,
                doc_id=doc_id, regex=regex, scan_seconds=scan_seconds, dry_run=True,
            )
            _lsplit_job_set(job_key, status="done", **payload)
            return

        if not has_real_qr_splits(best["markers"]):
            _lsplit_job_set(
                job_key,
                status="error",
                error=f"Keine QR-Split-Marker ({best['label']}, {best['total']} Seiten)",
                scan_seconds=scan_seconds,
            )
            return

        consume = Path(consume_dir)
        if not consume.exists():
            _lsplit_job_set(
                job_key, status="error",
                error=f"Consume-Ordner nicht gefunden: {consume}",
            )
            return

        staging = work_dir / "parts"
        staging.mkdir(parents=True, exist_ok=True)
        _lsplit_job_set(job_key, status="running", message="Teile lokal splitten…")
        parts = split_pdf_at_markers(
            str(local_pdf),
            staging,
            best["markers"],
            best["total"],
            source_basename=orig_name,
        )
        if not parts:
            _lsplit_job_set(job_key, status="error", error="Keine Split-Teile erzeugt")
            return

        _lsplit_job_set(job_key, status="running", message=f"{len(parts)} Teile nach consume/ verschieben…")
        for p in parts:
            src = Path(p["path"])
            dest = consume / p["filename"]
            shutil.move(str(src), str(dest))
            p["path"] = str(dest)

        detail = "; ".join(
            f"{p['barcode']} S.{p['from_page']}–{p['to_page']}" for p in parts
        )
        _lsplit_job_set(
            job_key,
            status="done",
            ok=True,
            dry_run=False,
            document_id=doc_id,
            source=best["label"],
            scan_seconds=scan_seconds,
            scan_meta=best.get("scan_meta") or {},
            parts=parts,
            message=(
                f"{len(parts)} Teil(e) nach {consume} geschrieben "
                f"({best['label']}, {scan_seconds}s) — Pipeline startet ({detail})"
            ),
        )
    except Exception as e:
        log.exception("Legacy-Split Job #%s", doc_id)
        _lsplit_job_set(
            job_key,
            status="error",
            error=str(e),
            scan_seconds=round(time.monotonic() - t0, 1),
        )
    finally:
        _legacy_split_cleanup(work_dir)


async def _run_legacy_split_bg(
    doc_id: int,
    regex: str,
    dry_run: bool,
    consume_dir: str,
    job_key: str,
) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _LEGACY_SPLIT_EXECUTOR,
        functools.partial(
            _run_legacy_split_job, doc_id, regex, dry_run, consume_dir, job_key,
        ),
    )


def resolve_group_ids(group_names: list[str]) -> list[int]:
    try:
        result = pl_get("/groups/")
        name_to_id = {g["name"].lower(): g["id"] for g in result.get("results", [])}
        return [name_to_id[n.lower()] for n in group_names if n.lower() in name_to_id]
    except Exception as e:
        log.warning(f"Gruppen-Auflösung fehlgeschlagen: {e}")
        return []


def get_correspondent_id_by_name(name: str) -> Optional[int]:
    """Sucht Korrespondent in Paperless by name (iexact) — gibt ID zurück oder None."""
    try:
        r = pl_get("/correspondents/", {"name__iexact": name})
        results = r.get("results", [])
        for item in results:
            if item["name"].lower() == name.lower():
                return item["id"]
    except Exception:
        pass
    return None


def create_correspondent(name: str, match_strings: list[str],
                         is_insensitive: bool = True) -> int:
    match_str  = "|".join(match_strings)
    payload = {
        "name": name,
        "match": match_str,
        "matching_algorithm": 0,
        "is_insensitive": is_insensitive,
    }
    payload.update(_default_permissions())
    result = pl_post("/correspondents/", payload)
    return result["id"]


def update_correspondent_match(paperless_id: int, match_strings: list[str]):
    match_str = "|".join(match_strings)
    pl_patch(f"/correspondents/{paperless_id}/", {"match": match_str})


def _set_document_type_on_documents(doc_ids: list[int], dt_name: str) -> None:
    """Dokumenttyp auf Dokumente setzen — nur wenn noch keiner gesetzt ist."""
    if not doc_ids or not dt_name:
        return
    # Dokumenttyp-ID ermitteln oder anlegen
    try:
        result = pl_get("/document_types/", {"name__iexact": dt_name})
        results = result.get("results", [])
        if results:
            dt_id = results[0]["id"]
        else:
            # Neu anlegen
            payload = {"name": dt_name, "matching_algorithm": 0}
            payload.update(_default_permissions())
            new_dt = pl_post("/document_types/", payload)
            dt_id = new_dt.get("id")
        if not dt_id:
            log.warning("Dokumenttyp '%s' konnte nicht aufgelöst werden", dt_name)
            return
        for doc_id in doc_ids:
            try:
                doc = pl_get(f"/documents/{doc_id}/")
                if doc.get("document_type"):
                    log.info("Dok #%s hat bereits Dokumenttyp — nicht überschreiben", doc_id)
                    continue
                patch = {"document_type": dt_id}
                patch.update(_default_permissions())
                pl_patch(f"/documents/{doc_id}/", patch)
                log.info("Dok #%s Dokumenttyp gesetzt: '%s' (ID %s)", doc_id, dt_name, dt_id)
            except Exception as e:
                log.warning("Dokumenttyp setzen für Dok #%s fehlgeschlagen: %s", doc_id, e)
    except Exception as e:
        log.warning("_set_document_type_on_documents fehlgeschlagen: %s", e)


def _resolve_or_create_doctype_id(dt_name: str) -> Optional[int]:
    """Dokumenttyp-ID aus Paperless holen oder neu anlegen."""
    if not dt_name:
        return None
    try:
        result = pl_get("/document_types/", {"name__iexact": dt_name})
        results = result.get("results", [])
        if results:
            return results[0]["id"]
        payload = {"name": dt_name, "matching_algorithm": 0}
        payload.update(_default_permissions())
        new_dt = pl_post("/document_types/", payload)
        dt_id = new_dt.get("id")
        log.info("Dokumenttyp '%s' neu angelegt (ID %s)", dt_name, dt_id)
        return dt_id
    except Exception as e:
        log.warning("Dokumenttyp '%s' auflösen fehlgeschlagen: %s", dt_name, e)
        return None


def _set_document_type_on_documents_by_id(doc_ids: list[int], dt_id: int) -> None:
    """Dokumenttyp via ID setzen — nur wenn noch keiner gesetzt."""
    for doc_id in doc_ids:
        try:
            doc = pl_get(f"/documents/{doc_id}/")
            if doc.get("document_type"):
                log.info("Dok #%s hat bereits Dokumenttyp — nicht überschreiben", doc_id)
                continue
            patch = {"document_type": dt_id}
            patch.update(_default_permissions())
            pl_patch(f"/documents/{doc_id}/", patch)
            log.info("Dok #%s Dokumenttyp ID %s gesetzt", doc_id, dt_id)
        except Exception as e:
            log.warning("Dokumenttyp setzen für Dok #%s fehlgeschlagen: %s", doc_id, e)


def assign_documents_to_correspondent(doc_ids: list[int], correspondent_id: int):
    """Bulk-Edit: mehrere Dokumente einem Korrespondenten zuweisen."""
    if not doc_ids:
        return
    payload = {
        "documents": doc_ids,
        "method": "set_correspondent",
        "parameters": {"correspondent": correspondent_id},
    }
    pl_post("/documents/bulk_edit/", payload)
    log.info(f"Bulk-Edit: Dokumente {doc_ids} → Korrespondent ID {correspondent_id}")


def remove_tag_from_documents(doc_ids: list[int], tag_name: str):
    """Einen Tag von Dokumenten entfernen via Bulk Edit."""
    try:
        tags_result = pl_get("/tags/", {"name__iexact": tag_name})
        if not tags_result.get("count"):
            return
        tag_id = tags_result["results"][0]["id"]
        pl_post("/documents/bulk_edit/", {
            "documents": doc_ids,
            "method": "remove_tag",
            "parameters": {"tag": tag_id},
        })
    except Exception as e:
        log.warning("Tag-Entfernung '%s' fehlgeschlagen: %s", tag_name, e)


def add_tag_to_documents(doc_ids: list[int], tag_name: str) -> None:
    """Einen Tag auf Dokumenten setzen via Bulk Edit."""
    if not doc_ids:
        return
    try:
        tags_result = pl_get("/tags/", {"name__iexact": tag_name})
        if tags_result.get("count"):
            tag_id = tags_result["results"][0]["id"]
        else:
            created = pl_post("/tags/", {"name": tag_name, "color": "#f59e0b", "matching_algorithm": 1})
            tag_id = created.get("id")
        if not tag_id:
            return
        pl_post("/documents/bulk_edit/", {
            "documents": doc_ids,
            "method": "add_tag",
            "parameters": {"tag": tag_id},
        })
    except Exception as e:
        log.warning("Tag '%s' setzen fehlgeschlagen: %s", tag_name, e)


def remove_all_pending_tags(doc_ids: list[int]) -> None:
    """ALLE pending-Tags entfernen beim Freigeben.
    Entfernt: pending_review, pending_qs, pending_new_correspondent, pending_brillenpass
    """
    for tag_name in ALL_PENDING_TAGS:
        remove_tag_from_documents(doc_ids, tag_name)
    log.info("Alle pending-Tags entfernt von Dok %s", doc_ids)


# ══════════════════════════════════════════════
# APPROVAL LOGIC
# ══════════════════════════════════════════════

def _ensure_manifest_entries(ordner_liste: list[str], korrespondent_name: str) -> None:
    """Für neue Ordner einen minimalen Manifest-Eintrag als 'pending' anlegen.
    Bestehende Einträge werden nicht überschrieben.
    """
    manifest_path = Path(os.environ.get("MANIFEST_PATH",
        "/opt/paperless-scripts/training/manifest.json"))
    if not manifest_path.exists():
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        existing_pfade = {e.get("pfad") for e in data.get("ordner", [])}
        changed = False
        for pfad in ordner_liste:
            if pfad in existing_pfade:
                continue
            # Neuer Ordner → minimaler pending-Eintrag
            new_ordner = {
                "pfad": pfad,
                "beschreibung": f"Automatisch angelegt für Korrespondent: {korrespondent_name}",
                "abgrenzung": "",
                "erlaubte_tags": [],
                "verbotene_tags": [],
                "erlaubte_dokumenttypen": [],
                "max_tags": 4,
                "pending": True,   # Markierung für paper.manager
            }
            data["ordner"].append(new_ordner)
            # Storage Path auch in Paperless anlegen
            _ensure_storage_path(pfad)
            changed = True
            log.info("Manifest: Neuer pending-Eintrag für Ordner '%s'", pfad)
        if changed:
            manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
    except Exception as e:
        log.warning("Manifest-Eintrag für '%s' fehlgeschlagen: %s", ordner_liste, e)


def _storage_path_template(pfad: str, *, with_correspondent: bool = True) -> str:
    """Paperless-ngx new-style path template (Jinja {{ }} placeholders)."""
    if with_correspondent:
        return f"{pfad}/{{{{ created_year }}}}/{{{{ correspondent }}}}/{{{{ title }}}}"
    return f"{pfad}/{{{{ created_year }}}}/{{{{ title }}}}"


def _ensure_storage_path(pfad: str) -> None:
    """Storage Path in Paperless anlegen falls nicht vorhanden."""
    try:
        existing = pl_get("/storage_paths/", {"name__iexact": pfad})
        if existing.get("count", 0) > 0:
            return
        template = _storage_path_template(pfad)
        payload = {"name": pfad, "path": template}
        payload.update(_default_permissions())
        result = pl_post("/storage_paths/", payload)
        log.info("Storage Path angelegt: '%s' (ID %s)", pfad, result.get("id"))
    except Exception as e:
        log.warning("Storage Path '%s' anlegen fehlgeschlagen: %s", pfad, e)


def _apply_audit_classification(doc_id: int) -> None:
    """Holt die letzte sanitized-Klassifizierung aus dem Audit-Log
    und patcht Tags, Storage Path und Custom Fields auf das Dokument —
    falls post_consume gecrasht ist bevor der PATCH ausgeführt wurde.
    """
    audit_path = Path(os.environ.get("AUDIT_LOG",
        "/opt/paperless-scripts/training/audit_log.jsonl"))
    if not audit_path.exists():
        return

    # Letzten sanitized-Eintrag für diese doc_id finden
    last_entry = None
    try:
        with open(audit_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("document_id") == doc_id and e.get("stage") == "sanitized":
                        last_entry = e.get("data", {})
                except json.JSONDecodeError:
                    pass
    except Exception as ex:
        log.warning("Audit-Log lesen fehlgeschlagen für Dok #%s: %s", doc_id, ex)
        return

    if not last_entry:
        log.info("Kein sanitized-Eintrag im Audit-Log für Dok #%s", doc_id)
        return

    patch = {}

    # Tags setzen
    tag_names = last_entry.get("tags", [])
    if tag_names:
        tag_ids = []
        for tn in tag_names:
            try:
                r = pl_get("/tags/", {"name__iexact": tn})
                if r.get("results"):
                    tag_ids.append(r["results"][0]["id"])
            except Exception:
                pass
        if tag_ids:
            # Bestehende Tags holen und ergänzen
            try:
                doc = pl_get(f"/documents/{doc_id}/")
                existing = doc.get("tags", [])
                merged = list(set(existing + tag_ids))
                patch["tags"] = merged
            except Exception:
                patch["tags"] = tag_ids

    # Storage Path setzen
    ordner = last_entry.get("ordner", "")
    if ordner:
        sp_id = _get_or_create_storage_path(ordner)
        if sp_id:
            patch["storage_path"] = sp_id

    # Custom Fields: Betrag
    betrag_raw = last_entry.get("betrag", "")
    if betrag_raw:
        import re as _re
        m = _re.search(r"[\d']+\.?\d*", betrag_raw.replace("'", ""))
        if m:
            try:
                patch.setdefault("custom_fields", [])
                cf_betrag = int(os.environ.get("CF_BETRAG_ID", "1"))
                patch["custom_fields"].append({"field": cf_betrag, "value": m.group()})
            except Exception:
                pass

    if not patch:
        log.info("Audit-Log: nichts zu patchen für Dok #%s", doc_id)
        return

    try:
        pl_patch(f"/documents/{doc_id}/", patch)
        log.info("Audit-Log Patch für Dok #%s: Tags=%s Ordner=%s",
                 doc_id, tag_names, ordner)
    except Exception as ex:
        log.warning("Audit-Log Patch fehlgeschlagen für Dok #%s: %s", doc_id, ex)


def _get_or_create_storage_path(pfad: str) -> Optional[int]:
    """Storage Path ID holen oder anlegen."""
    try:
        r = pl_get("/storage_paths/", {"name__iexact": pfad})
        if r.get("results"):
            return r["results"][0]["id"]
        # Anlegen
        result = pl_post("/storage_paths/", {"name": pfad, "path": _storage_path_template(pfad)})
        return result.get("id")
    except Exception as ex:
        log.warning("Storage Path '%s' nicht gefunden/angelegt: %s", pfad, ex)
        return None


def _register_new_correspondent(
    *,
    name: str,
    kuerzel: str,
    varianten: list,
    match_list: list,
    default_dt: str,
    default_dt_id: Optional[int],
    ordner: list,
    notiz: str,
    extr_muster: dict | None = None,
    erwartungen: dict | None = None,
    identifikatoren: dict | None = None,
) -> int:
    """Korrespondent in Paperless + correspondents.json anlegen. Gibt Paperless-ID zurück."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Name fehlt")
    kuerzel = (kuerzel or "").strip().upper()

    corr_map = load_corr_map()
    if any(e.get("name", "").strip().lower() == name.lower() for e in corr_map.get("eintraege", [])):
        raise HTTPException(409, f"Korrespondent «{name}» existiert bereits in correspondents.json")

    if kuerzel and not _check_kuerzel_unique(kuerzel, exclude_name=name, corr_map=corr_map):
        raise HTTPException(409, f"Kürzel «{kuerzel}» wird bereits verwendet")

    errors, warnings = _validate_correspondent_entry(
        name=name,
        match_strings=match_list,
        varianten=varianten,
        typische_ordner=ordner,
        corr_map=corr_map,
        identifikatoren=identifikatoren,
    )
    if errors:
        raise HTTPException(409, f"Validation fehlgeschlagen: {'; '.join(errors)}")
    if warnings:
        log.warning("Korrespondent '%s' — Warnings: %s", name, "; ".join(warnings))

    paperless_id = get_correspondent_id_by_name(name)
    if paperless_id:
        log.info("Korrespondent '%s' existiert in Paperless (ID %s) — Map-Eintrag wird verknüpft", name, paperless_id)
    else:
        paperless_id = create_correspondent(name, match_list)

    if default_dt and not default_dt_id:
        default_dt_id = _resolve_or_create_doctype_id(default_dt)

    new_entry = {
        "name": name,
        "kuerzel": kuerzel,
        "varianten": varianten,
        "match": match_list,
        "matching_algorithm": "any",
        "default_dokumenttyp": default_dt,
        "default_dokumenttyp_id": default_dt_id,
        "typische_ordner": ordner,
        "notiz": notiz,
        "extraktion_muster": extr_muster or {},
        "erwartungen": erwartungen or {},
        "identifikatoren": _normalize_identifikatoren(identifikatoren),
        "_paperless": {
            "id": paperless_id,
            "is_insensitive": True,
            "owner": None,
            "permissions": {
                "view":   {"users": [], "groups": PAPERLESS_VIEW_GROUPS},
                "change": {"users": [], "groups": PAPERLESS_CHANGE_GROUPS},
            },
        },
    }
    corr_map["eintraege"].append(new_entry)
    save_corr_map(corr_map)
    _ensure_manifest_entries(ordner, name)
    return paperless_id


def approve_neu(entry: dict, decision: ReviewDecision) -> str:
    """Neuen Korrespondenten anlegen: in Paperless + in correspondents.json."""
    name            = decision.canonical_name or entry["vorgeschlagener_eintrag"]["name"]
    varianten       = decision.varianten or entry["vorgeschlagener_eintrag"].get("varianten", [])
    match_list      = decision.match_strings or entry["vorgeschlagener_eintrag"].get("match", [])
    default_dt      = (decision.default_dokumenttyp or decision.typ or
                       entry["vorgeschlagener_eintrag"].get("default_dokumenttyp", ""))
    default_dt_id   = decision.default_dokumenttyp_id or entry["vorgeschlagener_eintrag"].get("default_dokumenttyp_id")
    ordner          = decision.typische_ordner or entry["vorgeschlagener_eintrag"].get("typische_ordner", [])
    notiz           = decision.notiz or entry["vorgeschlagener_eintrag"].get("notiz", "")
    extr_muster     = decision.extraktion_muster or entry["vorgeschlagener_eintrag"].get("extraktion_muster", {})
    erwartungen     = decision.erwartungen or entry["vorgeschlagener_eintrag"].get("erwartungen", {})
    kuerzel         = (decision.kuerzel or entry["vorgeschlagener_eintrag"].get("kuerzel") or "").strip().upper()

    paperless_id = _register_new_correspondent(
        name=name,
        kuerzel=kuerzel,
        varianten=varianten,
        match_list=match_list,
        default_dt=default_dt,
        default_dt_id=default_dt_id,
        ordner=ordner,
        notiz=notiz,
        extr_muster=extr_muster,
        erwartungen=erwartungen,
        identifikatoren=decision.identifikatoren or entry["vorgeschlagener_eintrag"].get("identifikatoren"),
    )

    doc_ids = entry.get("source_document_ids", [])
    assign_documents_to_correspondent(doc_ids, paperless_id)
    remove_all_pending_tags(doc_ids)

    if default_dt_id and doc_ids:
        _set_document_type_on_documents_by_id(doc_ids, default_dt_id)
    elif default_dt and doc_ids:
        _set_document_type_on_documents(doc_ids, default_dt)

    for doc_id in doc_ids:
        _apply_audit_classification(doc_id)

    return f"Korrespondent '{name}' angelegt (Paperless-ID {paperless_id}), {len(doc_ids)} Dokumente zugewiesen"


def approve_assign_existing(entry: dict, ziel_name: str) -> str:
    """Pending-Dokumente einem bereits freigegebenen Korrespondenten zuweisen — kein Neuanlegen."""
    ziel_name = (ziel_name or "").strip()
    if not ziel_name:
        raise HTTPException(400, "Ziel-Korrespondent fehlt")

    corr_map = load_corr_map()
    ziel = next((e for e in corr_map.get("eintraege", []) if e["name"] == ziel_name), None)
    if not ziel:
        raise HTTPException(404, f"Korrespondent '{ziel_name}' nicht in correspondents.json")

    paperless_id = ziel.get("_paperless", {}).get("id")
    if not paperless_id:
        paperless_id = get_correspondent_id_by_name(ziel_name)
        if paperless_id:
            ziel.setdefault("_paperless", {})["id"] = paperless_id
            save_corr_map(corr_map)
    if not paperless_id:
        raise HTTPException(409, f"Korrespondent '{ziel_name}' hat keine Paperless-ID")

    pending_name = (entry.get("vorgeschlagener_eintrag") or {}).get("name", "").strip()
    if pending_name and pending_name.lower() != ziel_name.lower():
        varianten = list(ziel.get("varianten") or [])
        if pending_name.lower() not in {v.lower() for v in varianten}:
            varianten.append(pending_name)
            ziel["varianten"] = varianten
            save_corr_map(corr_map)
            try:
                match_str = "|".join(ziel.get("match", []))
                pl_patch(f"/correspondents/{paperless_id}/", {"match": match_str})
            except Exception as ex:
                log.warning("Paperless match nach Varianten-Ergänzung: %s", ex)

    doc_ids = entry.get("source_document_ids", [])
    assign_documents_to_correspondent(doc_ids, paperless_id)
    remove_all_pending_tags(doc_ids)
    for doc_id in doc_ids:
        _apply_audit_classification(doc_id)

    return (
        f"{len(doc_ids)} Dokument(e) → bestehender Korrespondent «{ziel_name}» "
        f"(Paperless #{paperless_id}) — kein Duplikat angelegt"
    )


def approve_ergaenzung(entry: dict, decision: ReviewDecision) -> str:
    """Bestehenden Korrespondenten in der Map um Varianten ergänzen."""
    ziel_name      = entry["ziel_name"]
    # Fallback-Kette: UI-Eingabe → pending-Felder → vorgeschlagener_eintrag
    vorschlag      = entry.get("vorgeschlagener_eintrag", {})
    neue_varianten = (decision.varianten
                      or entry.get("neue_varianten")
                      or vorschlag.get("varianten", []))
    neue_match     = (decision.match_strings
                      or entry.get("neue_match")
                      or vorschlag.get("match", []))

    corr_map = load_corr_map()
    ziel = next((e for e in corr_map["eintraege"] if e["name"] == ziel_name), None)
    if not ziel:
        raise HTTPException(404, f"Ziel-Korrespondent '{ziel_name}' nicht in Map gefunden")

    # Map ergänzen
    for v in neue_varianten:
        if v not in ziel["varianten"]:
            ziel["varianten"].append(v)
    for m in neue_match:
        if m not in ziel["match"]:
            ziel["match"].append(m)
    save_corr_map(corr_map)

    # Paperless ID anlegen falls noch nicht vorhanden (analog approve_merge)
    paperless_id = ziel.get("_paperless", {}).get("id")
    if not paperless_id:
        paperless_id = (
            get_correspondent_id_by_name(ziel_name)
            or create_correspondent(
                ziel_name,
                ziel.get("match", []),
                is_insensitive=ziel.get("_paperless", {}).get("is_insensitive", True),
            )
        )
        if paperless_id:
            ziel.setdefault("_paperless", {})["id"] = paperless_id
            save_corr_map(corr_map)

    doc_ids = entry.get("source_document_ids", [])
    if paperless_id:
        update_correspondent_match(paperless_id, ziel["match"])
        assign_documents_to_correspondent(doc_ids, paperless_id)
        remove_all_pending_tags(doc_ids)
    else:
        # Anlegen fehlgeschlagen — wenigstens Tag entfernen
        remove_all_pending_tags(doc_ids)

    return f"Korrespondent '{ziel_name}' ergänzt um {len(neue_varianten)} Varianten, {len(doc_ids)} Dokumente aktualisiert"


def approve_merge(entry: dict, decision: ReviewDecision) -> str:
    """LLM-Erkennung als Alias eines bestehenden Korrespondenten behandeln."""
    ziel_name  = decision.merge_ziel_name or entry.get("merge_ziel_name")
    neue_match = decision.match_strings or (
        entry.get("vorgeschlagener_eintrag", {}).get("match", [])
    )

    if not ziel_name:
        raise HTTPException(400, "merge_ziel_name fehlt")

    corr_map = load_corr_map()
    ziel = next((e for e in corr_map["eintraege"] if e["name"] == ziel_name), None)
    if not ziel:
        raise HTTPException(404, f"Merge-Ziel '{ziel_name}' nicht gefunden")

    # Neue Match-Strings dem Ziel hinzufügen
    for m in neue_match:
        if m not in ziel["match"]:
            ziel["match"].append(m)
    # Varianten des Vorschlags übernehmen
    for v in entry.get("vorgeschlagener_eintrag", {}).get("varianten", []):
        if v not in ziel["varianten"]:
            ziel["varianten"].append(v)
    save_corr_map(corr_map)

    # Paperless aktualisieren — ID anlegen falls noch nicht vorhanden
    paperless_id = ziel.get("_paperless", {}).get("id")
    if not paperless_id:
        # Ziel existiert in Map aber noch nicht in Paperless → anlegen
        paperless_id = (
            get_correspondent_id_by_name(ziel_name)
            or create_correspondent(
                ziel_name,
                ziel.get("match", []),
                is_insensitive=ziel.get("_paperless", {}).get("is_insensitive", True),
            )
        )
        if paperless_id:
            ziel.setdefault("_paperless", {})["id"] = paperless_id
            save_corr_map(corr_map)

    doc_ids = entry.get("source_document_ids", [])
    if paperless_id:
        update_correspondent_match(paperless_id, ziel["match"])
        assign_documents_to_correspondent(doc_ids, paperless_id)
        remove_all_pending_tags(doc_ids)
    else:
        # Anlegen fehlgeschlagen — wenigstens Tag entfernen
        remove_all_pending_tags(doc_ids)

    return f"Merged in '{ziel_name}', Paperless-ID {paperless_id}"


# ══════════════════════════════════════════════
# API ENDPUNKTE
# ══════════════════════════════════════════════

# ── Document Review Queue ────────────────────────────────────────────────────
DOCUMENT_REVIEW_QUEUE = Path(os.environ.get(
    "DOCUMENT_REVIEW_QUEUE",
    "/opt/paperless-scripts/training/document_review_queue.jsonl"
))


def _norm_doc_id(doc_id) -> int | None:
    try:
        return int(doc_id)
    except (TypeError, ValueError):
        return None


def _merge_doc_review_grund(existing: list | None, new: list | None) -> list:
    return list(dict.fromkeys([*(existing or []), *(new or [])]))


def _compact_document_review_queue(entries: list[dict]) -> tuple[list[dict], bool]:
    """Fall 1: höchstens ein pending-Eintrag pro document_id."""
    pending_by_doc: dict[int, dict] = {}
    other: list[dict] = []
    changed = False
    for e in entries:
        if e.get("status") != "pending":
            other.append(e)
            continue
        did = _norm_doc_id(e.get("document_id"))
        if did is None:
            other.append(e)
            continue
        if did not in pending_by_doc:
            merged = dict(e)
            merged["document_id"] = did
            pending_by_doc[did] = merged
        else:
            changed = True
            base = pending_by_doc[did]
            base["grund"] = _merge_doc_review_grund(base.get("grund"), e.get("grund"))
            if not base.get("pfad"):
                base["pfad"] = e.get("pfad", "")
            if not base.get("title"):
                base["title"] = e.get("title", "")
            b1 = (base.get("begruendung") or "").strip()
            b2 = (e.get("begruendung") or "").strip()
            if b2 and b2 not in b1:
                base["begruendung"] = f"{b1}\n{b2}".strip() if b1 else b2
            if (e.get("timestamp") or "") > (base.get("timestamp") or ""):
                base["timestamp"] = e["timestamp"]
    return other + list(pending_by_doc.values()), changed


@contextmanager
def _document_review_queue_lock():
    lock_path = DOCUMENT_REVIEW_QUEUE.parent / ".document_review_queue.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def load_document_review_queue(compact: bool = True) -> list[dict]:
    if not DOCUMENT_REVIEW_QUEUE.exists():
        return []
    entries = []
    with open(DOCUMENT_REVIEW_QUEUE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if compact and entries:
        compacted, changed = _compact_document_review_queue(entries)
        if changed:
            save_document_review_queue(compacted)
            return compacted
    return entries


def save_document_review_queue(entries: list[dict]) -> None:
    DOCUMENT_REVIEW_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with open(DOCUMENT_REVIEW_QUEUE, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _doc_has_correspondent(doc_id: int) -> bool:
    try:
        doc = pl_get(f"/documents/{doc_id}/")
        return bool(doc.get("correspondent"))
    except Exception as e:
        log.debug("Korrespondent-Check Dok #%s fehlgeschlagen: %s", doc_id, e)
        return False


def _corr_escalation_needs_doc_review(doc_id: int) -> bool:
    """Fall 2: Korrespondent bereits gesetzt → kein erneutes Doc-Review."""
    if _doc_has_correspondent(doc_id):
        log.info(
            "Doc-Review übersprungen — Dok #%s hat bereits Korrespondenten (Endverarbeitung)",
            doc_id,
        )
        return False
    return True


def _apply_doc_review_enqueue_update(
    e: dict,
    *,
    pfad: str,
    confidence: str,
    grund: list[str],
    title: str,
    begruendung: str,
    now: str,
) -> None:
    e["grund"] = _merge_doc_review_grund(e.get("grund"), grund)
    e["pfad"] = pfad or e.get("pfad", "")
    e["confidence"] = confidence or e.get("confidence", "mittel")
    e["title"] = title or e.get("title", "")
    if begruendung:
        old = (e.get("begruendung") or "").strip()
        e["begruendung"] = begruendung if not old else (
            begruendung if begruendung in old else f"{old}\n{begruendung}".strip()
        )
    e["timestamp"] = now


def enqueue_document_review_entry(
    doc_id: int,
    *,
    pfad: str = "",
    confidence: str = "mittel",
    grund: list[str] | None = None,
    title: str = "",
    begruendung: str = "",
    requeue_after_approve: bool = True,
) -> None:
    """Dokument in Document-Review-Queue einreihen (Dedupe per doc_id)."""
    grund = grund or []
    did = _norm_doc_id(doc_id)
    if did is None:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    with _document_review_queue_lock():
        entries = load_document_review_queue(compact=False)
        entries, _ = _compact_document_review_queue(entries)

        for e in entries:
            if _norm_doc_id(e.get("document_id")) != did:
                continue
            if e.get("status") == "pending":
                _apply_doc_review_enqueue_update(
                    e, pfad=pfad, confidence=confidence, grund=grund,
                    title=title, begruendung=begruendung, now=now,
                )
                save_document_review_queue(entries)
                log.info("Document Review Queue: ID=%s aktualisiert (Dedupe), Grund=%s", did, e["grund"])
                return
            if e.get("status") == "approved" and requeue_after_approve:
                _apply_doc_review_enqueue_update(
                    e, pfad=pfad, confidence=confidence, grund=grund,
                    title=title, begruendung=begruendung, now=now,
                )
                e["status"] = "pending"
                e.pop("reviewed_at", None)
                save_document_review_queue(entries)
                log.info(
                    "Document Review Queue: ID=%s reaktiviert (approved→pending), Grund=%s",
                    did, e["grund"],
                )
                return
            if e.get("status") == "approved":
                log.info("Document Review Queue: ID=%s bereits approved — kein neuer Eintrag", did)
                return

        entries.append({
            "document_id": did,
            "pfad": pfad,
            "confidence": confidence,
            "grund": grund,
            "title": title,
            "begruendung": begruendung,
            "status": "pending",
            "timestamp": now,
        })
        save_document_review_queue(entries)
        log.info("Document Review Queue: ID=%s neu eingereiht, Grund=%s", did, grund)


def _escalate_corr_reject_to_doc_review(entry: dict) -> None:
    """Korrespondenten-Review abgelehnt → Dokument in Document-Review mit pending_review."""
    doc_ids = entry.get("source_document_ids") or []
    if not doc_ids:
        return
    remove_tag_from_documents(doc_ids, PENDING_NEW_CORR_TAG)
    raw_name = entry.get("llm_raw") or entry.get("vorgeschlagener_eintrag", {}).get("name", "")
    docs_for_review: list[int] = []
    for doc_id in doc_ids:
        did = _norm_doc_id(doc_id)
        if did is not None and _corr_escalation_needs_doc_review(did):
            docs_for_review.append(did)
    if docs_for_review:
        add_tag_to_documents(docs_for_review, PENDING_REVIEW_TAG)
    for doc_id in docs_for_review:
        title, pfad, conf = "", "", "mittel"
        try:
            doc = pl_get(f"/documents/{doc_id}/")
            title = doc.get("title") or ""
            if doc.get("storage_path"):
                sp = pl_get(f"/storage_paths/{doc['storage_path']}/")
                pfad = sp.get("name", "")
        except Exception as e:
            log.warning("Dokument %s für Escalation nicht lesbar: %s", doc_id, e)
        enqueue_document_review_entry(
            doc_id,
            pfad=pfad,
            confidence=conf,
            grund=["Korrespondent-Review abgelehnt", "Korrespondent offen"],
            title=title,
            begruendung=f"Korrespondent '{raw_name}' nicht freigegeben — bitte Korrespondent zuweisen",
            requeue_after_approve=True,
        )


def _approved_correspondents_for_docs() -> list[dict]:
    """Korrespondenten für Dokument-Review: in Map + Paperless-ID.

    Bereits freigegebene Korrespondenten bleiben sichtbar — auch wenn noch
    Duplikat-Pending-Einträge in der Review-Warteschlange existieren.
    """
    corr_map = load_corr_map()
    results = []
    dirty = False
    for e in corr_map.get("eintraege", []):
        pl_id = e.get("_paperless", {}).get("id")
        if not pl_id:
            pl_id = get_correspondent_id_by_name(e.get("name", ""))
            if pl_id:
                e.setdefault("_paperless", {})["id"] = pl_id
                dirty = True
        if not pl_id:
            continue
        results.append({
            "id": pl_id,
            "name": e["name"],
            "kuerzel": e.get("kuerzel", ""),
        })
    if dirty:
        save_corr_map(corr_map)
    results.sort(key=lambda x: x["name"].lower())
    return results


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}




@app.get("/api/pending", response_class=JSONResponse)
def api_pending():
    """Alle pending-Einträge als JSON."""
    entries = load_pending()
    pending = [{"index": i, **e} for i, e in enumerate(entries) if e.get("status") == "pending"]
    return {"count": len(pending), "entries": pending}


@app.get("/api/pending-beziehungen", response_class=JSONResponse)
def api_pending_beziehungen():
    """Alle pending Beziehungs-Vorschläge."""
    path = Path(os.environ.get("PENDING_BEZIEHUNGEN_JSONL",
        "/opt/paperless-scripts/training/pending_beziehungen.jsonl"))
    if not path.exists():
        return {"count": 0, "entries": []}
    entries = []
    for i, line in enumerate(path.read_text(encoding="utf-8").strip().split("\n")):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            entries.append({"index": i, **e})
        except Exception:
            pass
    pending = [e for e in entries if e.get("status") == "pending"]
    return {"count": len(pending), "entries": pending}


@app.post("/api/pending-beziehungen/{index}/approve")
def api_approve_beziehung(index: int, body: dict = Body(...)):
    """Beziehungs-Vorschlag freigeben — optional in correspondents.json speichern.
    body: { als_regel: bool, beziehung: {...editierter Vorschlag...} }
    """
    path = Path(os.environ.get("PENDING_BEZIEHUNGEN_JSONL",
        "/opt/paperless-scripts/training/pending_beziehungen.jsonl"))
    if not path.exists():
        raise HTTPException(404, "pending_beziehungen.jsonl nicht gefunden")

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    if index >= len(lines):
        raise HTTPException(404, f"Index {index} nicht gefunden")

    entry = json.loads(lines[index])
    bez   = body.get("beziehung") or {
        "person":           entry.get("person", ""),
        "bezeichnung":      entry.get("bezeichnung", ""),
        "referenznummer":   entry.get("referenznummer", ""),
        "erlaubte_doctypen": entry.get("erlaubte_doctypen", []),
        "ordner":           entry.get("ordner", ""),
    }

    # Als Regel speichern → in correspondents.json eintragen
    if body.get("als_regel", False):
        corr_name = entry.get("korrespondent", "")
        corr_map  = load_corr_map()
        corr_entry = next(
            (e for e in corr_map.get("eintraege", []) if e["name"] == corr_name), None)
        if not corr_entry:
            raise HTTPException(404, f"Korrespondent '{corr_name}' nicht gefunden")
        beziehungen = corr_entry.setdefault("beziehungen", [])
        # Prüfen ob bereits vorhanden (gleiche person + bezeichnung)
        def _bez_konflikt(b: dict, neu: dict) -> bool:
            """True wenn b und neu als Duplikat gelten."""
            if b.get("person") != neu.get("person"):
                return False
            # Gleiche Bezeichnung
            if b.get("bezeichnung") and b.get("bezeichnung") == neu.get("bezeichnung"):
                return True
            # Gleiche Referenznummer (nicht leer)
            ref_b = (b.get("referenznummer") or "").strip()
            ref_n = (neu.get("referenznummer") or "").strip()
            if ref_b and ref_n and ref_b == ref_n:
                return True
            return False

        exists = any(_bez_konflikt(b, bez) for b in beziehungen)
        if exists:
            log.info("Beziehung bereits vorhanden — nicht doppelt gespeichert")
        else:
            beziehungen.append(bez)
            save_corr_map(corr_map)
            log.info("Beziehung gespeichert: %s → person=%s", corr_name, bez.get("person"))

    # Status auf approved setzen
    entry["status"] = "approved"
    lines[index] = json.dumps(entry, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {"status": "approved", "als_regel": body.get("als_regel", False)}


@app.post("/api/pending-beziehungen/{index}/reject")
def api_reject_beziehung(index: int):
    """Beziehungs-Vorschlag ablehnen."""
    path = Path(os.environ.get("PENDING_BEZIEHUNGEN_JSONL",
        "/opt/paperless-scripts/training/pending_beziehungen.jsonl"))
    if not path.exists():
        raise HTTPException(404, "pending_beziehungen.jsonl nicht gefunden")
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    if index >= len(lines):
        raise HTTPException(404, f"Index {index} nicht gefunden")
    entry = json.loads(lines[index])
    entry["status"] = "rejected"
    lines[index] = json.dumps(entry, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"status": "rejected"}


def _load_brillenpaesse() -> dict:
    if not BRILLENPAESSE_JSON.exists():
        return {"version": "1.0", "eintraege": []}
    return json.loads(BRILLENPAESSE_JSON.read_text(encoding="utf-8"))


def _save_brillenpaesse(data: dict) -> None:
    BRILLENPAESSE_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = BRILLENPAESSE_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(BRILLENPAESSE_JSON)


def _load_pending_brillenpass_lines() -> list[str]:
    if not PENDING_BRILLENPASS_JSONL.exists():
        return []
    return [ln for ln in PENDING_BRILLENPASS_JSONL.read_text(encoding="utf-8").split("\n") if ln.strip()]


def _save_pending_brillenpass_lines(lines: list[str]) -> None:
    PENDING_BRILLENPASS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    PENDING_BRILLENPASS_JSONL.write_text(
        ("\n".join(lines) + "\n") if lines else "", encoding="utf-8",
    )


def _get_letzte_brillenpass_version(person_id: str) -> dict | None:
    data = _load_brillenpaesse()
    for entry in data.get("eintraege", []):
        if entry.get("person_id") == person_id:
            vers = entry.get("versionen") or []
            return latest_brillenpass_version(vers)
    return None


def _resolve_person_anzeigename(person_id: str) -> str:
    if FAMILY_JSON.exists():
        data = json.loads(FAMILY_JSON.read_text(encoding="utf-8"))
        for p in data.get("personen", []):
            if p.get("id") == person_id:
                return p.get("anzeigename") or p.get("name") or person_id
    return person_id


def _queue_brillenpass_review(
    *,
    vorschlag: dict,
    person_id: str,
    anzeigename: str,
    korrespondent: str,
    document_id: int | None = None,
    source: str = "manual",
) -> bool:
    lines = _load_pending_brillenpass_lines()
    gueltig_ab = (vorschlag or {}).get("gueltig_ab")
    for ln in lines:
        try:
            e = json.loads(ln)
            if e.get("status") != "pending":
                continue
            if document_id and e.get("document_id") == document_id:
                return False
            if not document_id and source == "manual":
                ev = e.get("vorschlag") or {}
                if (
                    e.get("source") == "manual"
                    and e.get("person_id") == person_id
                    and e.get("korrespondent") == korrespondent
                    and ev.get("gueltig_ab") == gueltig_ab
                ):
                    return False
        except json.JSONDecodeError:
            continue

    entry = {
        "status": "pending",
        "source": source,
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "document_id": document_id,
        "person_id": person_id,
        "anzeigename": anzeigename,
        "korrespondent": korrespondent,
        "vorschlag": vorschlag,
        "letzte_version": _get_letzte_brillenpass_version(person_id),
    }
    lines.append(json.dumps(entry, ensure_ascii=False))
    _save_pending_brillenpass_lines(lines)
    return True


def _find_brillenpass_entry(data: dict, person_id: str) -> dict | None:
    for e in data.get("eintraege", []):
        if e.get("person_id") == person_id:
            return e
    return None


def _repair_brillenpass_store(data: dict) -> bool:
    """Sortiert Versionen, normalisiert Datumsformate, aktuell = neuestes gültig_ab."""
    changed = False
    for e in data.get("eintraege", []):
        vers = list(e.get("versionen") or [])
        for v in vers:
            iso = normalize_gueltig_ab_iso(v.get("gueltig_ab"))
            if iso and iso != v.get("gueltig_ab"):
                v["gueltig_ab"] = iso
                changed = True
            if hydrate_messung_from_diagnose(v):
                changed = True
            if apply_brillenpass_doc_patches(v):
                changed = True
        deduped, dedup_changed = dedupe_brillenpass_versions_by_document(vers)
        if dedup_changed:
            vers = deduped
            changed = True
        for i, v in enumerate(vers):
            prev = vers[i - 1] if i > 0 else None
            new_diff = compute_brillenpass_diff(prev, v)
            if v.get("diff_zu_vorher") != new_diff:
                v["diff_zu_vorher"] = new_diff
                changed = True
        sorted_vers = sort_brillenpass_versions(vers)
        correct = resolve_brillenpass_aktuell(sorted_vers)
        if sorted_vers != (e.get("versionen") or []):
            e["versionen"] = sorted_vers
            changed = True
        if correct and e.get("aktuell") != correct:
            e["aktuell"] = correct
            changed = True
    if changed:
        _save_brillenpaesse(data)
    return changed


@app.get("/api/brillenpass/correspondents", response_class=JSONResponse)
def api_brillenpass_correspondents():
    """Korrespondenten mit brillenpass.aktiv — für manuelle Erfassung."""
    out = []
    for e in load_corr_map().get("eintraege", []):
        aktiv, _ = corr_supports_brillenpass(e)
        if aktiv:
            out.append({
                "name": e.get("name"),
                "vendor": (e.get("brillenpass") or {}).get("vendor"),
            })
    out.sort(key=lambda x: (x.get("name") or "").lower())
    return {"count": len(out), "eintraege": out}


@app.get("/api/brillenpass", response_class=JSONResponse)
def api_brillenpass_list():
    """Alle Brillenpässe — alle Versionen pro Person (chronologisch sortiert)."""
    data = _load_brillenpaesse()
    _repair_brillenpass_store(data)
    result = []
    for e in data.get("eintraege", []):
        vers = sort_brillenpass_versions(e.get("versionen") or [])
        aktuell = resolve_brillenpass_aktuell(vers)
        current = latest_brillenpass_version(vers)
        result.append({
            "person_id":    e.get("person_id"),
            "anzeigename":  e.get("anzeigename"),
            "aktuell":      aktuell,
            "version_count": len(vers),
            "versionen":    vers,
            "current":      current,
        })
    pending_lines = _load_pending_brillenpass_lines()
    pending_persons = set()
    for ln in pending_lines:
        try:
            pe = json.loads(ln)
            if pe.get("status") == "pending":
                pending_persons.add(pe.get("person_id"))
        except Exception:
            pass
    for r in result:
        r["pending_review"] = r["person_id"] in pending_persons
    return {"count": len(result), "eintraege": result}


@app.get("/api/brillenpass/parsers", response_class=JSONResponse)
def api_brillenpass_parsers():
    """Verfügbare Brillenpass-Parser (nach Dokumentformat) + Optiker-Vendors."""
    return {
        "parsers": [
            {
                "id": name,
                "label": PARSER_LABELS.get(name, name),
                "format": PARSER_FORMAT.get(name, ""),
                "vendor": PARSER_VENDOR.get(name, ""),
            }
            for name in PARSER_NAMES
        ],
        "vendors": [
            {
                "id": vid,
                "label": VENDOR_LABELS.get(vid, vid),
                "parsers": VENDOR_PARSERS[vid],
            }
            for vid in VENDOR_PARSERS
        ],
    }


@app.post("/api/brillenpass/parse")
def api_brillenpass_parse(body: dict = Body(...)):
    """OCR-Text oder Paperless-Dok-ID mit Parser parsen (ohne Review-Queue)."""
    parser_name = normalize_parser_name((body.get("parser") or "").strip())
    text = (body.get("text") or "").strip()
    doc_id = body.get("document_id")

    if doc_id:
        try:
            doc = pl_get(f"/documents/{int(doc_id)}/")
            text = (doc.get("content") or "").strip()
        except Exception as e:
            raise HTTPException(400, f"Dokument laden fehlgeschlagen: {e}") from e

    if not text:
        raise HTTPException(400, "text oder document_id mit OCR-Inhalt erforderlich")

    detected = detect_parser(text)
    if not parser_name:
        parser_name = detected or "fielmann_rechnung"

    if parser_name not in PARSER_NAMES:
        raise HTTPException(400, f"Unbekannter Parser: {parser_name}")

    result = parse_by_parser(parser_name, text) or {}
    if not has_brillenpass_values(result):
        return {
            "ok": False,
            "parser": parser_name,
            "detected_parser": detected,
            "result": result,
            "message": "Keine verwertbaren Werte erkannt",
        }
    return {
        "ok": True,
        "parser": parser_name,
        "detected_parser": detected or parser_name,
        "result": result,
    }


def _corr_entry_by_name(name: str) -> dict | None:
    key = (name or "").strip()
    if not key:
        return None
    for e in load_corr_map().get("eintraege", []):
        if (e.get("name") or "").strip() == key:
            return e
    return None


@app.post("/api/brillenpass/manual")
def api_brillenpass_manual(body: dict = Body(...)):
    """Manuelle Brillenpass-Erfassung → Review-Queue."""
    person_id = (body.get("person_id") or "").strip()
    korrespondent = (body.get("korrespondent") or "").strip()
    if not person_id:
        raise HTTPException(400, "person_id erforderlich")
    if not korrespondent:
        raise HTTPException(400, "korrespondent erforderlich")
    aktiv, _ = corr_supports_brillenpass(_corr_entry_by_name(korrespondent))
    if not aktiv:
        raise HTTPException(
            400,
            f"Korrespondent «{korrespondent}» hat kein brillenpass.aktiv — im Tab Korrespondenten aktivieren",
        )

    anzeigename = (body.get("anzeigename") or "").strip() or _resolve_person_anzeigename(person_id)
    doc_id = body.get("document_id")
    if doc_id is not None:
        try:
            doc_id = int(doc_id)
        except (TypeError, ValueError):
            doc_id = None

    vorschlag = body.get("vorschlag") or {}
    if body.get("gueltig_ab"):
        vorschlag["gueltig_ab"] = body["gueltig_ab"]
    vorschlag["korrespondent"] = korrespondent
    if body.get("parser"):
        vorschlag["parser"] = body["parser"]
    vorschlag.setdefault("fern", {"rechts": None, "links": None})
    vorschlag.setdefault("naehe", {"rechts": None, "links": None})
    vorschlag.setdefault("messung", {"rechts": None, "links": None})
    vorschlag.setdefault("glas", {"beschreibung": "", "index": None, "durchmesser": None, "beschichtungen": []})
    vorschlag.setdefault("extraktion", {"quelle": "manual", "confidence": "hoch"})

    if not has_brillenpass_values(vorschlag):
        raise HTTPException(400, "Mindestens ein Auge mit Sph oder Glas-Beschreibung erforderlich")

    if not _queue_brillenpass_review(
        vorschlag=vorschlag,
        person_id=person_id,
        anzeigename=anzeigename,
        korrespondent=korrespondent,
        document_id=doc_id,
        source="manual",
    ):
        raise HTTPException(409, "Bereits in Review-Queue (gleiche Person/Datum/Korrespondent)")

    if doc_id:
        add_tag_to_documents([doc_id], PENDING_BRILLENPASS_TAG)

    return {"ok": True, "message": f"Manueller Brillenpass für {anzeigename} zur Review eingereiht"}


async def _run_brillenpass_bg(doc_id: int, force: bool, parser_override: str) -> None:
    """Vision/Ollama in eigenem Thread-Pool — Default-Pool bleibt für HTTP frei."""
    from brillenpass_runner import brillenpass_job_run
    loop = asyncio.get_running_loop()
    fn = functools.partial(
        brillenpass_job_run, doc_id, force=force, parser_override=parser_override,
    )
    await loop.run_in_executor(_BG_EXECUTOR, fn)


@app.post("/api/brillenpass/trigger/{doc_id}")
def api_brillenpass_trigger(
    doc_id: int,
    background_tasks: BackgroundTasks,
    body: dict = Body(default={}),
):
    """Bestehendes Dokument nachträglich durch Brillenpass-Pipeline (async, Vision ~1–2 Min)."""
    from brillenpass_runner import (
        brillenpass_job_set,
        preflight_brillenpass_document,
    )

    force = bool(body.get("force", False))
    parser_override = (body.get("parser") or "").strip()

    try:
        pre = preflight_brillenpass_document(
            doc_id, force=force, parser_override=parser_override,
        )
    except Exception as e:
        log.exception("Brillenpass preflight #%s", doc_id)
        raise HTTPException(500, f"Brillenpass-Vorprüfung fehlgeschlagen: {e}") from e

    if not pre.get("ok"):
        raise HTTPException(400, pre.get("error", "Brillenpass-Trigger fehlgeschlagen"))

    img = "mit Vision" if pre.get("has_image") else "nur Parser (kein PDF-Bild)"
    person = pre.get("anzeigename") or pre.get("person_id") or "?"
    start_msg = f"Brillenpass für {person} ({img}) — Vision ~1–2 Min…"
    brillenpass_job_set(doc_id, status="running", message=start_msg)
    background_tasks.add_task(_run_brillenpass_bg, doc_id, force, parser_override)

    return {
        "ok": True,
        "async": True,
        "document_id": doc_id,
        "message": start_msg,
        "person_id": pre.get("person_id"),
        "person_match": pre.get("person_match"),
    }


@app.get("/api/brillenpass/trigger-status/{doc_id}")
def api_brillenpass_trigger_status(doc_id: int):
    """Status eines laufenden/kürzlich beendeten Brillenpass-Triggers (UI-Polling)."""
    from brillenpass_runner import _doc_in_pending_brillenpass, brillenpass_job_get

    job = brillenpass_job_get(doc_id)
    if job:
        return job
    pending = _doc_in_pending_brillenpass(doc_id)
    if pending:
        return {
            "status": "done",
            "document_id": doc_id,
            "message": f"In Review-Liste ({pending.get('anzeigename', '?')})",
        }
    return {
        "status": "unknown",
        "document_id": doc_id,
        "message": "Kein aktueller Lauf — Review-Liste oder Log prüfen",
    }


@app.get("/api/brillenpass/{person_id}", response_class=JSONResponse)
def api_brillenpass_detail(person_id: str):
    data = _load_brillenpaesse()
    entry = _find_brillenpass_entry(data, person_id)
    if not entry:
        raise HTTPException(404, f"Kein Brillenpass für '{person_id}'")
    return entry


@app.get("/api/brillenpass-review", response_class=JSONResponse)
def api_brillenpass_review():
    entries = []
    for i, line in enumerate(_load_pending_brillenpass_lines()):
        try:
            e = json.loads(line)
            entries.append({"index": i, **e})
        except Exception:
            pass
    pending = [e for e in entries if e.get("status") == "pending"]
    return {"count": len(pending), "entries": pending}


@app.post("/api/brillenpass-review/{index}")
def api_brillenpass_review_action(index: int, body: dict = Body(...)):
    """action=approve|reject — bei approve optional vorschlag überschreiben."""
    lines = _load_pending_brillenpass_lines()
    if index >= len(lines):
        raise HTTPException(404, f"Index {index} nicht gefunden")

    entry = json.loads(lines[index])
    action = (body.get("action") or "approve").lower()
    now = datetime.now(timezone.utc).isoformat()

    if action == "reject":
        entry["status"] = "rejected"
        entry["reviewed_at"] = now
        lines[index] = json.dumps(entry, ensure_ascii=False)
        _save_pending_brillenpass_lines(lines)
        doc_id = entry.get("document_id")
        if doc_id:
            remove_tag_from_documents([doc_id], PENDING_BRILLENPASS_TAG)
        return {"status": "rejected"}

    if action != "approve":
        raise HTTPException(400, "action muss approve oder reject sein")

    vorschlag = body.get("vorschlag") or entry.get("vorschlag") or {}
    person_id = entry.get("person_id", "")
    anzeigename = entry.get("anzeigename", "")
    korrespondent = entry.get("korrespondent") or vorschlag.get("korrespondent", "")
    doc_id = entry.get("document_id")

    if not has_brillenpass_values(vorschlag):
        raise HTTPException(400, "Mindestens ein Auge mit Sph oder Glas-Index erforderlich")

    gueltig_ab = (vorschlag.get("gueltig_ab") or "").strip()
    if not gueltig_ab:
        gueltig_ab = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    data = _load_brillenpaesse()
    bp_entry = _find_brillenpass_entry(data, person_id)
    if not bp_entry:
        bp_entry = {"person_id": person_id, "anzeigename": anzeigename, "aktuell": gueltig_ab, "versionen": []}
        data.setdefault("eintraege", []).append(bp_entry)

    vers = bp_entry.setdefault("versionen", [])
    prev = chronological_prev_version(vers, gueltig_ab)
    dup_idx = None
    if doc_id:
        for i, v in enumerate(vers):
            if doc_id in collect_document_ids(v):
                dup_idx = i
                break
    if dup_idx is None:
        dup_idx = find_brillenpass_period_duplicate(
            vers, gueltig_ab, korrespondent, max_days=BRILLENPASS_DEDUP_DAYS,
        )
    incoming = {
        "gueltig_ab": gueltig_ab,
        "korrespondent": korrespondent,
        "document_id": doc_id,
        "document_ids": [doc_id] if doc_id else [],
        "auftrag": vorschlag.get("auftrag", ""),
        "rechnung": vorschlag.get("rechnung", ""),
        "fern": vorschlag.get("fern") or {"rechts": None, "links": None},
        "naehe": vorschlag.get("naehe") or {"rechts": None, "links": None},
        "messung": vorschlag.get("messung") or {"rechts": None, "links": None},
        "glas": vorschlag.get("glas") or {},
        "pd": vorschlag.get("pd") or {"rechts": None, "links": None},
        "extraktion": vorschlag.get("extraktion") or {"quelle": "review", "confidence": "hoch"},
    }
    deduped = False
    if dup_idx is not None:
        prev_for_diff = vers[dup_idx - 1] if dup_idx > 0 else None
        merged = merge_brillenpass_version(vers[dup_idx], incoming)
        merged["diff_zu_vorher"] = compute_brillenpass_diff(prev_for_diff, merged)
        vers[dup_idx] = merged
        version = merged
        deduped = True
    else:
        version = {
            "id": build_version_id(gueltig_ab, korrespondent),
            **incoming,
            "diff_zu_vorher": compute_brillenpass_diff(prev, incoming),
        }
        vers.append(version)
    vers[:] = sort_brillenpass_versions(vers)
    bp_entry["aktuell"] = resolve_brillenpass_aktuell(vers) or gueltig_ab
    if anzeigename:
        bp_entry["anzeigename"] = anzeigename
    _save_brillenpaesse(data)

    entry["status"] = "approved"
    entry["reviewed_at"] = now
    lines[index] = json.dumps(entry, ensure_ascii=False)
    _save_pending_brillenpass_lines(lines)

    if doc_id:
        remove_tag_from_documents([doc_id], PENDING_BRILLENPASS_TAG)
        try:
            note = f"Brillenpass aktualisiert ({gueltig_ab}) — {korrespondent}"
            pl_post(f"/documents/{doc_id}/notes/", {"note": note})
        except Exception as e:
            log.warning("Brillenpass-Notiz fehlgeschlagen: %s", e)

    return {
        "status": "approved",
        "version_id": version.get("id", build_version_id(gueltig_ab, korrespondent)),
        "diff": version.get("diff_zu_vorher", {}),
        "deduped": deduped,
    }


@app.post("/api/legacy-split/trigger/{doc_id}")
async def api_legacy_split_trigger(
    doc_id: int,
    background_tasks: BackgroundTasks,
    body: dict = Body(default={}),
):
    """Paperless-Dokument per QR splitten — async (UI pollt Status, kein nginx-Timeout)."""
    dry_run = bool(body.get("dry_run", False))
    sync = bool(body.get("sync", False))
    regex = normalize_legacy_qr_regex((body.get("regex") or LEGACY_SPLIT_QR_REGEX).strip())
    consume_dir = str(body.get("consume_dir") or PAPERLESS_CONSUME_DIR)
    job_key = _lsplit_job_key(doc_id, dry_run)

    if sync:
        loop = asyncio.get_running_loop()
        t_scan = time.monotonic()
        await loop.run_in_executor(
            _LEGACY_SPLIT_EXECUTOR,
            functools.partial(_run_legacy_split_job, doc_id, regex, dry_run, consume_dir, job_key),
        )
        job = _lsplit_job_get(job_key)
        if job.get("status") == "error":
            raise HTTPException(400, job.get("error", "Legacy-Split fehlgeschlagen"))
        job["scan_seconds"] = job.get("scan_seconds") or round(time.monotonic() - t_scan, 1)
        return job

    existing = _lsplit_job_get(job_key)
    if existing.get("status") == "running":
        age = time.time() - existing.get("updated_at", 0)
        if age < 120:
            return {
                "async": True,
                "document_id": doc_id,
                "status": "running",
                "message": existing.get("message", "QR-Scan läuft bereits…"),
            }
        log.warning("Legacy-Split Job %s hängt (%ds) — Neustart", job_key, int(age))

    _lsplit_job_set(
        job_key,
        status="running",
        message="QR-Scan startet…",
        document_id=doc_id,
        dry_run=dry_run,
    )
    background_tasks.add_task(
        _run_legacy_split_bg, doc_id, regex, dry_run, consume_dir, job_key,
    )
    return {
        "async": True,
        "document_id": doc_id,
        "status": "running",
        "message": "QR-Scan läuft (Ghostscript ~10s)…",
    }


@app.get("/api/legacy-split/trigger-status/{doc_id}")
def api_legacy_split_trigger_status(doc_id: int, dry_run: bool = True):
    """Status eines Legacy-Split-Jobs (UI-Polling)."""
    job = _lsplit_job_get(_lsplit_job_key(doc_id, dry_run))
    if not job:
        return {
            "status": "unknown",
            "document_id": doc_id,
            "message": "Kein Job — Vorschau erneut starten",
        }
    return job


@app.patch("/api/brillenpass/{person_id}")
def api_brillenpass_patch(person_id: str, body: dict = Body(...)):
    """Manuelle Korrektur einer bestehenden Version (version_id im Body)."""
    version_id = (body.get("version_id") or "").strip()
    if not version_id:
        raise HTTPException(400, "version_id erforderlich")

    data = _load_brillenpaesse()
    bp_entry = _find_brillenpass_entry(data, person_id)
    if not bp_entry:
        raise HTTPException(404, f"Kein Brillenpass für '{person_id}'")

    vers = bp_entry.get("versionen") or []
    idx = next((i for i, v in enumerate(vers) if v.get("id") == version_id), None)
    if idx is None:
        raise HTTPException(404, f"Version '{version_id}' nicht gefunden")

    prev = vers[idx - 1] if idx > 0 else None
    updated = {**vers[idx], **(body.get("version") or {})}
    updated["diff_zu_vorher"] = compute_brillenpass_diff(prev, updated)
    vers[idx] = updated
    sorted_vers = sort_brillenpass_versions(vers)
    bp_entry["versionen"] = sorted_vers
    bp_entry["aktuell"] = resolve_brillenpass_aktuell(sorted_vers) or bp_entry.get("aktuell")
    _save_brillenpaesse(data)
    return {"status": "updated", "version": updated}


@app.get("/api/correspondents", response_class=JSONResponse)
def api_correspondents():
    """Kanonisierungs-Map als JSON."""
    return load_corr_map()


@app.post("/api/correspondents")
def api_create_correspondent(body: dict = Body(...)):
    """Korrespondent manuell anlegen — gleiche Validierung wie Review-Freigabe, ohne Pending-Eintrag."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name erforderlich")
    match_list = body.get("match") or body.get("match_strings") or []
    if not match_list:
        match_list = [name.lower()]
    paperless_id = _register_new_correspondent(
        name=name,
        kuerzel=(body.get("kuerzel") or "").strip().upper(),
        varianten=body.get("varianten") or [],
        match_list=match_list,
        default_dt=(body.get("default_dokumenttyp") or body.get("typ") or "").strip(),
        default_dt_id=body.get("default_dokumenttyp_id"),
        ordner=body.get("typische_ordner") or [],
        notiz=(body.get("notiz") or "").strip(),
        extr_muster=body.get("extraktion_muster"),
        erwartungen=body.get("erwartungen"),
        identifikatoren=body.get("identifikatoren"),
    )
    log.info("Manuell angelegt: '%s' (Paperless #%s)", name, paperless_id)
    return {
        "status": "created",
        "name": name,
        "paperless_id": paperless_id,
        "message": f"Korrespondent «{name}» angelegt (Paperless #{paperless_id})",
    }


@app.get("/api/document/{doc_id}", response_class=JSONResponse)
def api_document(doc_id: int):
    """Dokument-Details aus Paperless — mit aufgelösten Select-Feldern."""
    try:
        doc = pl_get(f"/documents/{doc_id}/")

        # Select-Felder auflösen: interne Option-ID → lesbarer Wert
        try:
            cf_defs = pl_get("/custom_fields/").get("results", [])
            cf_options = {}
            for cf in cf_defs:
                opts = cf.get("extra_data", {}).get("select_options", [])
                if opts:
                    cf_options[cf["id"]] = {o["id"]: o["label"] for o in opts}

            resolved = []
            for cf in doc.get("custom_fields", []):
                field_id = cf.get("field")
                raw_value = cf.get("value")
                display = raw_value
                if field_id in cf_options and raw_value:
                    display = cf_options[field_id].get(raw_value, raw_value)
                resolved.append({"field": field_id, "value": raw_value, "display": display})
            doc["custom_fields"] = resolved
        except Exception:
            pass  # Fallback: originale Werte behalten

        # Begründung aus Document Review Queue ergänzen
        try:
            entries = load_document_review_queue()
            for e in entries:
                if e.get("document_id") == doc_id and e.get("status") == "pending":
                    doc["_begruendung"] = e.get("begruendung", "")
                    break
        except Exception:
            pass

        return doc
    except requests.HTTPError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))


@app.post("/api/review/merge")
def api_review_merge(request: Request, body: dict = Body(...)):
    """Merge: zwei pending-Einträge zusammenführen.
    body: { index_a: int, index_b: int }
    Ergebnis: index_a behält beide doc_ids, index_b wird gelöscht.
    """
    _check_internal_token(request)
    index_a = body.get("index_a")
    index_b = body.get("index_b")
    if index_a is None or index_b is None:
        raise HTTPException(400, "index_a und index_b erforderlich")
    if index_a == index_b:
        raise HTTPException(400, "index_a und index_b müssen verschieden sein")

    entries = load_pending()
    pending = [(i, e) for i, e in enumerate(entries) if e.get("status") == "pending"]

    if index_a >= len(pending) or index_b >= len(pending):
        raise HTTPException(404, "Eintrag nicht gefunden")

    orig_a, entry_a = pending[index_a]
    orig_b, entry_b = pending[index_b]

    # doc_ids zusammenführen (dedupliziert)
    ids_a = entry_a.get("source_document_ids", [])
    ids_b = entry_b.get("source_document_ids", [])
    merged_ids = list(dict.fromkeys(ids_a + ids_b))  # Reihenfolge erhalten, dedupliziert
    entries[orig_a]["source_document_ids"] = merged_ids

    # LLM-Namen und Confidence aus besserem Eintrag übernehmen
    conf_a = entry_a.get("llm_confidence", 0)
    conf_b = entry_b.get("llm_confidence", 0)
    if conf_b > conf_a:
        entries[orig_a]["llm_raw"] = entry_b.get("llm_raw", entry_a.get("llm_raw"))
        entries[orig_a]["llm_confidence"] = conf_b

    # Bei unterschiedlichen Namen: anderen Namen als Variante vorschlagen
    va = entries[orig_a].get("vorgeschlagener_eintrag") or {}
    vb = entry_b.get("vorgeschlagener_eintrag") or {}
    name_a = (va.get("name") or "").strip()
    name_b = (vb.get("name") or "").strip()
    if name_b and name_b.lower() != name_a.lower():
        varianten = list(va.get("varianten") or [])
        for extra in [name_b] + (vb.get("varianten") or []):
            if extra and extra.lower() not in {v.lower() for v in varianten} and extra.lower() != name_a.lower():
                varianten.append(extra)
        va["varianten"] = varianten
        entries[orig_a]["vorgeschlagener_eintrag"] = va

    # Eintrag B auf merged setzen
    entries[orig_b]["status"] = "merged"
    entries[orig_b]["merged_into"] = orig_a

    save_pending(entries)
    log.info("Merge: pending[%s] + pending[%s] → doc_ids=%s", index_a, index_b, merged_ids)
    return {"status": "merged", "doc_ids": merged_ids, "count": len(merged_ids)}


@app.post("/api/correspondents/merge-paperless")
def api_merge_paperless_correspondents(request: Request, body: dict = Body(...)):
    """Merge: zwei Paperless-Korrespondenten zusammenführen.
    body: { keep_id: int, merge_id: int }
    Alle Dokumente von merge_id werden keep_id zugewiesen, merge_id wird gelöscht.
    Correspondents.json wird bereinigt.
    """
    _check_internal_token(request)
    keep_id = body.get("keep_id")
    merge_id = body.get("merge_id")
    if not keep_id or not merge_id:
        raise HTTPException(400, "keep_id und merge_id erforderlich")
    if keep_id == merge_id:
        raise HTTPException(400, "keep_id und merge_id müssen verschieden sein")

    # Dokumente von merge_id holen
    try:
        docs = pl_get("/documents/", {"correspondent__id": merge_id, "page_size": 500})
        doc_ids = [d["id"] for d in docs.get("results", [])]
    except Exception as e:
        raise HTTPException(502, f"Dokumente holen fehlgeschlagen: {e}")

    # Dokumente auf keep_id umschreiben
    if doc_ids:
        assign_documents_to_correspondent(doc_ids, keep_id)
        log.info("Merge Paperless: %d Dokumente von ID %s → ID %s", len(doc_ids), merge_id, keep_id)

    # merge_id in Paperless löschen
    try:
        _http.delete(
            f"{PAPERLESS_API_URL}/correspondents/{merge_id}/",
            headers=_headers(), timeout=15
        )
        log.info("Merge Paperless: Korrespondent ID %s gelöscht", merge_id)
    except Exception as e:
        log.warning("Löschen von Korrespondent %s fehlgeschlagen: %s", merge_id, e)

    # correspondents.json: merge_id entfernen, keep_id Varianten ergänzen
    corr_map = load_corr_map()
    keep_entry = next((e for e in corr_map.get("eintraege", [])
                       if e.get("_paperless", {}).get("id") == keep_id), None)
    merge_entry = next((e for e in corr_map.get("eintraege", [])
                        if e.get("_paperless", {}).get("id") == merge_id), None)

    if merge_entry and keep_entry:
        # Varianten und Match-Strings übernehmen
        existing_var = set(v.lower() for v in keep_entry.get("varianten", []))
        for v in merge_entry.get("varianten", []) + [merge_entry.get("name", "")]:
            if v and v.lower() not in existing_var:
                keep_entry.setdefault("varianten", []).append(v)
                existing_var.add(v.lower())
        existing_match = set(m.lower() for m in keep_entry.get("match", []))
        for m in merge_entry.get("match", []):
            if m and m.lower() not in existing_match:
                keep_entry.setdefault("match", []).append(m)
                existing_match.add(m.lower())
        # merge_entry entfernen
        corr_map["eintraege"] = [e for e in corr_map["eintraege"]
                                  if e.get("_paperless", {}).get("id") != merge_id]
        save_corr_map(corr_map)
        log.info("correspondents.json: '%s' in '%s' aufgegangen",
                 merge_entry.get("name"), keep_entry.get("name"))

    return {
        "status": "merged",
        "kept": keep_id,
        "deleted": merge_id,
        "documents_reassigned": len(doc_ids),
    }


@app.post("/api/review/{index}")
def api_review(index: int, decision: ReviewDecision, request: "Request"):
    """Freigabe / Ablehnung eines Pending-Eintrags."""
    # Auth via Paperless-Session-Middleware (require_paperless_session)
    entries = load_pending()
    pending_entries = [(i, e) for i, e in enumerate(entries) if e.get("status") == "pending"]

    if index >= len(pending_entries):
        raise HTTPException(404, "Eintrag nicht gefunden")

    original_index, entry = pending_entries[index]
    now = datetime.now(timezone.utc).isoformat()

    try:
        if decision.action == "reject":
            _escalate_corr_reject_to_doc_review(entry)
            entries[original_index]["status"] = "rejected"
            entries[original_index]["reviewed_by"] = decision.reviewed_by
            entries[original_index]["reviewed_at"] = now
            save_pending(entries)
            return {"status": "rejected", "message": "Abgelehnt — Dokument(e) in Document-Review eingereiht"}

        if decision.action == "assign_existing":
            ziel = (decision.assign_to_existing_name or decision.merge_ziel_name or "").strip()
            if not ziel:
                raise HTTPException(400, "assign_to_existing_name erforderlich")
            msg = approve_assign_existing(entry, ziel)
            entries[original_index]["status"] = "approved"
            entries[original_index]["assigned_to"] = ziel
            entries[original_index]["reviewed_by"] = decision.reviewed_by
            entries[original_index]["reviewed_at"] = now
            save_pending(entries)
            return {"status": "approved", "message": msg}

        aktion = entry.get("aktion", "neu")
        if decision.action in ("approve", "approve_neu") and aktion == "neu":
            msg = approve_neu(entry, decision)
        elif decision.action in ("approve", "approve_ergaenzung") and aktion == "ergaenzung":
            msg = approve_ergaenzung(entry, decision)
        elif decision.action in ("approve", "approve_merge") and aktion == "merge_into":
            msg = approve_merge(entry, decision)
        elif decision.action == "approve_neu" and aktion == "merge_into":
            # "Als NEU anlegen statt Merge" — merge_into als neuen Korrespondenten behandeln
            msg = approve_neu(entry, decision)
        else:
            raise HTTPException(400, f"Ungültige Kombination: action={decision.action}, aktion={aktion}")

        entries[original_index]["status"] = "approved"
        entries[original_index]["reviewed_by"] = decision.reviewed_by
        entries[original_index]["reviewed_at"] = now
        save_pending(entries)
        return {"status": "approved", "message": msg}

    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"Fehler bei Review-Aktion: {e}")
        raise HTTPException(500, str(e))


@app.put("/api/correspondents/{name}")
def api_edit_correspondent(name: str, body: dict = Body(...)):
    """Bestehenden Korrespondenten in correspondents.json mutieren.
    Felder: varianten, match, typ, typische_ordner, notiz
    Name selbst ist nicht änderbar (ist der Key).
    """
    corr_map = load_corr_map()
    entry = next((e for e in corr_map.get("eintraege", []) if e["name"] == name), None)
    if not entry:
        raise HTTPException(404, f"Korrespondent '{name}' nicht gefunden")

    # Validation — eigenen Eintrag ausschliessen (exclude_name)
    new_match    = body.get("match",           entry.get("match", []))
    new_varianten = body.get("varianten",       entry.get("varianten", []))
    new_ordner   = body.get("typische_ordner", entry.get("typische_ordner", []))
    errors, warnings = _validate_correspondent_entry(
        name=name,
        match_strings=new_match,
        varianten=new_varianten,
        typische_ordner=new_ordner,
        corr_map=corr_map,
        exclude_name=name,  # eigenen Eintrag nicht als Konflikt werten
        identifikatoren=body.get("identifikatoren", entry.get("identifikatoren")),
    )
    if errors:
        raise HTTPException(409, f"Validation fehlgeschlagen: {'; '.join(errors)}")
    if warnings:
        log.warning("Edit '%s' — Warnings: %s", name, "; ".join(warnings))

    # kuerzel Uniqueness prüfen
    new_kuerzel = (body.get("kuerzel") or "").strip().upper()
    if new_kuerzel and not _check_kuerzel_unique(new_kuerzel, exclude_name=name, corr_map=corr_map):
        raise HTTPException(409, f"Kürzel '{new_kuerzel}' wird bereits von einem anderen Korrespondenten verwendet")

    # Nur erlaubte Felder updaten
    for field in ["varianten", "match", "default_dokumenttyp", "default_dokumenttyp_id",
                  "typische_ordner", "notiz", "extraktion_muster", "erwartungen",
                  "fix_tags", "verbotene_doctypen", "verbotene_ordner", "verbotene_tags",
                  "nicht_verwechseln_mit", "beziehungen", "kuerzel", "identifikatoren", "brillenpass"]:
        if field in body:
            val = body[field]
            if field == "kuerzel":
                val = (body[field] or "").strip().upper()
            elif field == "nicht_verwechseln_mit":
                val = _normalize_string_list(body[field])
            elif field == "identifikatoren":
                val = _normalize_identifikatoren(body[field])
            elif field == "brillenpass":
                val = _normalize_brillenpass(body[field])
            entry[field] = val

    for bez in entry.get("beziehungen", []):
        _normalize_beziehung_fields(bez)

    # Defaults setzen falls neu
    entry.setdefault("fix_tags", [])
    entry.setdefault("verbotene_doctypen", [])
    entry.setdefault("verbotene_ordner", [])
    entry.setdefault("verbotene_tags", [])
    entry.setdefault("nicht_verwechseln_mit", [])
    entry.setdefault("beziehungen", [])
    entry.setdefault("kuerzel", "")
    entry.setdefault("identifikatoren", {"uid": [], "iban": [], "email": [], "telefon": []})

    # default_dokumenttyp_id synchronisieren falls nur Name geändert
    if "default_dokumenttyp" in body and "default_dokumenttyp_id" not in body:
        dt_id = _resolve_or_create_doctype_id(body["default_dokumenttyp"])
        if dt_id:
            entry["default_dokumenttyp_id"] = dt_id

    # Paperless match-string synchronisieren
    paperless_id = entry.get("_paperless", {}).get("id")
    if paperless_id and "match" in body:
        try:
            match_str = "|".join(body["match"])
            pl_patch(f"/correspondents/{paperless_id}/", {"match": match_str})
            log.info("Paperless Korrespondent %s match aktualisiert", paperless_id)
        except Exception as e:
            log.warning("Paperless match-update fehlgeschlagen: %s", e)

    save_corr_map(corr_map)
    return {"status": "updated", "name": name}


@app.get("/api/check-kuerzel", response_class=JSONResponse)
def api_check_kuerzel(kuerzel: str = "", exclude: str = ""):
    """Prüft ob ein Kürzel bereits vergeben ist.
    exclude: eigenen Korrespondenten-Namen ausschliessen (beim Edit).
    """
    if not kuerzel:
        return {"available": True}
    corr_map = load_corr_map()
    is_unique = _check_kuerzel_unique(kuerzel, exclude_name=exclude, corr_map=corr_map)
    return {"available": is_unique, "kuerzel": kuerzel.strip().upper()}


@app.get("/api/check-variant", response_class=JSONResponse)
def api_check_variant(name: str = "", exclude: str = ""):
    """Prüft ob ein Name/Variante/Match-String bereits in correspondents.json existiert.
    exclude: eigenen Korrespondenten-Namen ausschliessen (beim Edit).
    """
    if not name:
        return {"exists": False, "match": None}
    corr_map = load_corr_map()
    norm = name.lower().strip()
    for entry in corr_map.get("eintraege", []):
        if exclude and entry["name"] == exclude:
            continue
        all_names = [entry["name"]] + entry.get("varianten", []) + entry.get("match", [])
        for n in all_names:
            if n.lower().strip() == norm:
                return {"exists": True, "match": entry["name"]}
    return {"exists": False, "match": None}


@app.post("/api/validate-correspondent", response_class=JSONResponse)
def api_validate_correspondent(body: dict = Body(...)):
    """Vollständige Validation eines Korrespondenten-Eintrags.
    Gibt {errors: [...], warnings: [...]} zurück.
    Frontend zeigt diese VOR dem Freigeben an.
    """
    corr_map = load_corr_map()
    errors, warnings = _validate_correspondent_entry(
        name=body.get("name", ""),
        match_strings=body.get("match", []),
        varianten=body.get("varianten", []),
        typische_ordner=body.get("typische_ordner", []),
        corr_map=corr_map,
        exclude_name=body.get("exclude_name"),
        identifikatoren=body.get("identifikatoren"),
    )
    return {"errors": errors, "warnings": warnings, "valid": len(errors) == 0}


# PENDING_MODE Datei — persistente Einstellung
_PENDING_MODE_FILE = Path(os.environ.get(
    "PENDING_MODE_FILE",
    "/opt/paperless-scripts/training/pending_mode.txt"
))


def _get_pending_mode() -> str:
    """Aktuellen PENDING_MODE lesen (always/uncertain/never)."""
    if _PENDING_MODE_FILE.exists():
        mode = _PENDING_MODE_FILE.read_text().strip()
        if mode in ("always", "uncertain", "never"):
            return mode
    return os.environ.get("PENDING_MODE", "uncertain")


def _set_pending_mode(mode: str) -> None:
    """PENDING_MODE in Datei speichern UND in post_consume .env schreiben."""
    if mode not in ("always", "uncertain", "never"):
        raise ValueError(f"Ungültiger PENDING_MODE: {mode}")
    _PENDING_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PENDING_MODE_FILE.write_text(mode)
    # Auch in .env schreiben damit post_consume es liest
    env_path = Path(os.environ.get("PAPERLESS_ENV", "/opt/paperless/.env"))
    if env_path.exists():
        env = env_path.read_text(encoding="utf-8")
        if "PENDING_MODE=" in env:
            import re
            env = re.sub(r"PENDING_MODE=.*", "PENDING_MODE=" + mode, env)
        else:
            env += "\nPENDING_MODE=" + mode + "\n"
        env_path.write_text(env, encoding="utf-8")
    log.info("PENDING_MODE gesetzt: %s", mode)


@app.get("/api/config", response_class=JSONResponse)
def api_config(request: Request):
    """Konfiguration + Versionen für Frontend — einziger Init-Call."""
    def _rv(path: str, marker: str) -> str:
        try:
            for line in open(path):
                if line.strip().startswith(marker):
                    val = line.split("=")[1].strip().strip('"\'')
                    # Inline-Kommentar abschneiden: '12.3  # Beschreibung' → '12.3'
                    val = val.split("#")[0].strip().strip('"\'')
                    return val
        except Exception:
            pass
        return "?"
    base = "/opt/paperless-scripts"
    canonical = os.environ.get("PAPERLESS_URL", "http://localhost:8000")
    return {
        "paperless_url": _effective_paperless_url(request),
        "paperless_url_config": canonical,
        "pending_mode":  _get_pending_mode(),
        "versions": {
            "ui":             UI_VERSION,
            "backend":        __version__,
            "post_consume":   _rv(f"{base}/post_consume.py",   "POST_CONSUME_VERSION"),
            "pre_consume_sh": _rv(f"{base}/pre_consume.sh",    "# VERSION"),
            "pre_consume_qr": _rv(f"{base}/pre_consume_qr.py", "__version__"),
        },
    }


def _load_tags_json() -> dict:
    if TAGS_JSON.exists():
        return json.loads(TAGS_JSON.read_text(encoding="utf-8"))
    return {"version": "1.0", "tags": []}


def _save_tags_json(data: dict) -> None:
    TAGS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = TAGS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TAGS_JSON)


@app.post("/api/pending-mode")
def api_set_pending_mode(body: dict = Body(...)):
    """PENDING_MODE setzen: always / uncertain / never."""
    mode = (body.get("mode") or "").strip()
    try:
        _set_pending_mode(mode)
        return {"status": "ok", "pending_mode": mode}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/paperless/correspondents", response_class=JSONResponse)
def api_pl_correspondents():
    """Alle Korrespondenten direkt aus Paperless (für Merge-Dropdown)."""
    try:
        result = pl_get("/correspondents/", {"page_size": 200})
        return {"results": result.get("results", [])}
    except Exception as e:
        raise HTTPException(502, f"Paperless nicht erreichbar: {e}")


@app.get("/api/correspondents/approved-for-docs", response_class=JSONResponse)
def api_approved_correspondents_for_docs():
    """Korrespondenten für Dokument-Review-Zuweisung (Map + Paperless-ID, nicht pending-neu)."""
    return {"results": _approved_correspondents_for_docs()}


@app.get("/api/document-review", response_class=JSONResponse)
def api_document_review():
    """Alle Dokumente in der Document Review Queue."""
    entries = load_document_review_queue()
    pending = [{"index": i, **e} for i, e in enumerate(entries) if e.get("status") == "pending"]
    return {"count": len(pending), "entries": pending}


def _learn_from_reclassification(
    doc_id: int,
    new_ordner: str,
    new_tags: list[str],
    original_ordner: str,
    original_tags: list[str],
) -> None:
    """Lernkreislauf: Korrekturen aus Dokument-Review ins Manifest zurückschreiben.

    Wenn ein Dokument von Ordner A nach Ordner B verschoben wird:
    - Neue Tags werden als Vorschläge zum Manifest-Ordner B hinzugefügt
    - Audit-Log Eintrag
    """
    if new_ordner == original_ordner and set(new_tags) == set(original_tags):
        log.info("Kein Lerneffekt — keine Änderung (Dok #%s)", doc_id)
        return

    manifest_path = Path(os.environ.get("MANIFEST_PATH",
        "/opt/paperless-scripts/training/manifest.json"))
    if not manifest_path.exists():
        return

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        changed = False

        # Ziel-Ordner im Manifest finden und Tags als Vorschläge ergänzen
        for entry in data.get("ordner", []):
            if entry.get("pfad") != new_ordner:
                continue
            existing = entry.get("erlaubte_tags", [])
            existing_lower = {t.lower() for t in existing}
            added = []
            for tag in new_tags:
                if tag.lower() not in existing_lower:
                    existing.append(tag)
                    added.append(tag)
            entry["erlaubte_tags"] = existing
            if added:
                log.info("Manifest '%s': +Tags %s (via Dok-Review #%s)", new_ordner, added, doc_id)
                changed = True
            break
        else:
            # Ordner nicht im Manifest — als pending anlegen
            _ensure_manifest_entries([new_ordner], f"Dok #{doc_id} reklassifiziert")
            changed = True

        if changed:
            manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

        # Audit-Log
        audit_path = Path("/opt/paperless-scripts/training/audit_log.jsonl")
        import time as _t
        audit_entry = {
            "ts": _t.strftime("%Y-%m-%dT%H:%M:%S"),
            "action": "reclassify_learn",
            "doc_id": doc_id,
            "original_ordner": original_ordner,
            "new_ordner": new_ordner,
            "original_tags": original_tags,
            "new_tags": new_tags,
        }
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")

    except Exception as e:
        log.warning("_learn_from_reclassification fehlgeschlagen: %s", e)


def _feldprofil_for_document_type_id(document_type_id: int | None) -> dict:
    """feldprofil aus document_types.json für einen Paperless-Dokumenttyp."""
    if not document_type_id:
        return {}
    try:
        pl_dt = pl_get(f"/document_types/{document_type_id}/")
        dt_name = (pl_dt.get("name") or "").lower()
    except Exception:
        return {}
    dt_json_path = Path(os.environ.get("DOCUMENT_TYPES_JSON",
        "/opt/paperless-scripts/training/document_types.json"))
    if not dt_json_path.exists():
        return {}
    try:
        dt_data = json.loads(dt_json_path.read_text(encoding="utf-8"))
        for t in dt_data.get("typen", []):
            if t.get("name", "").lower() == dt_name:
                return t.get("feldprofil", {}) or {}
    except Exception as e:
        log.warning("feldprofil laden fehlgeschlagen: %s", e)
    return {}


def _merge_custom_fields(doc_id: int, updates: list[dict]) -> list[dict]:
    """Bestehende Custom Fields mit Review-Änderungen mergen."""
    doc = pl_get(f"/documents/{doc_id}/")
    merged = {cf["field"]: cf["value"] for cf in doc.get("custom_fields", []) if cf.get("field") is not None}
    for item in updates:
        field_id = item.get("field")
        if field_id is None:
            continue
        value = item.get("value")
        if value is None or str(value).strip() == "":
            merged.pop(field_id, None)
        else:
            merged[field_id] = value
    return [{"field": fid, "value": val} for fid, val in merged.items()]


def _validate_pflicht_custom_fields(feldprofil: dict, custom_fields: list[dict]) -> None:
    """Pflichtfelder aus feldprofil prüfen — HTTP 400 bei fehlenden Werten."""
    if not feldprofil:
        return
    values = {cf["field"]: cf.get("value") for cf in custom_fields}
    missing = []
    for key, cfg in feldprofil.items():
        if not cfg.get("pflicht"):
            continue
        fid = int(key)
        val = values.get(fid)
        if val is None or str(val).strip() == "":
            missing.append(str(key))
    if missing:
        raise HTTPException(400, f"Pflichtfelder fehlen: {', '.join(missing)}")


@app.post("/api/document-review/{index}")
def api_document_review_action(index: int, body: dict = Body(...)):
    """
    Aktionen auf Document Review Queue:
      action=approve  → pending_review Tag entfernen, Permissions sicherstellen
      action=reject   → Eintrag als rejected markieren (Dokument bleibt)
      action=reclassify → ordner/tags im Body → Paperless PATCH + Queue-Eintrag erledigt
    """
    entries = load_document_review_queue()
    pending = [(i, e) for i, e in enumerate(entries) if e.get("status") == "pending"]
    if index >= len(pending):
        raise HTTPException(404, "Eintrag nicht gefunden")

    orig_idx, entry = pending[index]
    doc_id = entry.get("document_id")
    action = body.get("action", "approve")
    now    = datetime.now(timezone.utc).isoformat()

    try:
        if action == "reject":
            entries[orig_idx]["status"] = "rejected"
            entries[orig_idx]["reviewed_at"] = now
            save_document_review_queue(entries)
            return {"status": "rejected"}

        if action in ("approve", "reclassify"):
            patch: dict = {}
            patch.update(_default_permissions())

            if action == "reclassify":
                # Manuelle Neuklassifizierung: Korrekturfelder aus Body
                if body.get("storage_path_id"):
                    patch["storage_path"] = body["storage_path_id"]
                if body.get("correspondent_id"):
                    patch["correspondent"] = body["correspondent_id"]
                if body.get("document_type_id"):
                    patch["document_type"] = body["document_type_id"]

            # Tags: bei Freigeben und Reklassifizieren (Auswahl ersetzt, pending wird unten entfernt)
            if "tag_ids" in body:
                patch["tags"] = list(dict.fromkeys(body["tag_ids"]))

            # Custom Fields aus Review-Formular (approve + reclassify)
            if "custom_fields" in body:
                merged_cfs = _merge_custom_fields(doc_id, body.get("custom_fields") or [])
                patch["custom_fields"] = merged_cfs

            # Belegdatum → Paperless-Feld «created» (Ausstellungsdatum)
            if body.get("created"):
                created = str(body.get("created") or "").strip()
                if created:
                    patch["created"] = created

            # Dokumenttitel (approve + reclassify)
            if body.get("title") is not None:
                new_title = str(body.get("title") or "").strip()
                if new_title:
                    patch["title"] = new_title[:128]

            # pending Tags entfernen (alle drei)
            try:
                doc = pl_get(f"/documents/{doc_id}/")
                existing_tags = doc.get("tags", [])
                all_pr_ids = set()
                for tag_name in ALL_PENDING_TAGS:
                    pr_tags = pl_get("/tags/", {"name__iexact": tag_name})
                    all_pr_ids.update(t["id"] for t in pr_tags.get("results", []))
                clean_tags = [t for t in existing_tags if t not in all_pr_ids]
                if "tags" not in patch:
                    patch["tags"] = clean_tags
                else:
                    patch["tags"] = [t for t in patch["tags"] if t not in all_pr_ids]
            except Exception as e:
                log.warning("Pending-Tag-Entfernung fehlgeschlagen: %s", e)

            pl_patch(f"/documents/{doc_id}/", patch)

            # Lernkreislauf: Bei reclassify → Manifest-Feedback
            if action == "reclassify" and body.get("storage_path_name"):
                _learn_from_reclassification(
                    doc_id=doc_id,
                    new_ordner=body.get("storage_path_name"),
                    new_tags=body.get("tag_names", []),
                    original_ordner=entry.get("ordner", ""),
                    original_tags=entry.get("tags", []),
                )

            entries[orig_idx]["status"] = "approved"
            entries[orig_idx]["reviewed_at"] = now
            save_document_review_queue(entries)
            return {"status": "approved", "doc_id": doc_id}

    except Exception as e:
        log.exception("Document Review Fehler: %s", e)
        raise HTTPException(500, str(e))


@app.get("/api/paperless/storage-paths", response_class=JSONResponse)
def api_storage_paths():
    """Alle Storage Paths aus Paperless (für Reklassifizierung)."""
    try:
        result = pl_get("/storage_paths/", {"page_size": 200})
        return {"results": result.get("results", [])}
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/paperless/custom-fields", response_class=JSONResponse)
def api_custom_fields():
    """Alle Custom Field Definitionen aus Paperless (inkl. Select-Optionen)."""
    try:
        result = pl_get("/custom_fields/", {"page_size": 200})
        return {"results": result.get("results", [])}
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/paperless/tags", response_class=JSONResponse)
def api_tags():
    """Alle Tags aus Paperless, angereichert mit ausschliessen aus tags.json."""
    try:
        result = pl_get("/tags/", {"page_size": 200})
        tags_data = _load_tags_json()
        meta_map = {t["name"].lower(): t for t in tags_data.get("tags", [])}
        enriched = []
        for t in result.get("results", []):
            entry = dict(t)
            entry["ausschliessen"] = meta_map.get(t["name"].lower(), {}).get("ausschliessen", [])
            enriched.append(entry)
        return {"results": enriched}
    except Exception as e:
        raise HTTPException(502, str(e))


@app.patch("/api/paperless/tags/{tag_id}")
def api_patch_tag(tag_id: int, body: dict = Body(...)):
    """Tag umbenennen und/oder ausschliessen-Keywords aktualisieren."""
    new_name      = (body.get("name") or "").strip()
    ausschliessen = body.get("ausschliessen", [])
    if new_name:
        try:
            pl_patch(f"/tags/{tag_id}/", {"name": new_name})
        except Exception as e:
            raise HTTPException(502, f"Paperless PATCH fehlgeschlagen: {e}")
    try:
        current_name = pl_get(f"/tags/{tag_id}/").get("name", new_name)
    except Exception:
        current_name = new_name
    tags_data = _load_tags_json()
    tags_list = tags_data.get("tags", [])
    found = False
    for t in tags_list:
        if t["name"].lower() == current_name.lower() or t.get("_paperless_id") == tag_id:
            t["name"] = current_name; t["ausschliessen"] = ausschliessen; t["_paperless_id"] = tag_id
            found = True; break
    if not found:
        tags_list.append({"name": current_name, "ausschliessen": ausschliessen, "_paperless_id": tag_id})
    tags_data["tags"] = tags_list
    _save_tags_json(tags_data)
    return {"status": "updated", "name": current_name, "ausschliessen": ausschliessen}


@app.post("/api/paperless/tags")
def api_create_tag(body: dict = Body(...)):
    """Neuen Tag in Paperless anlegen."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name fehlt")
    payload = {"name": name, "matching_algorithm": 0}
    payload.update(_default_permissions())
    try:
        result = pl_post("/tags/", payload)
        return result
    except Exception as e:
        raise HTTPException(502, str(e))


@app.delete("/api/paperless/tags/{tag_id}")
def api_delete_tag(tag_id: int):
    """Tag in Paperless löschen und aus tags.json entfernen."""
    try:
        tag = pl_get(f"/tags/{tag_id}/")
    except Exception as e:
        raise HTTPException(404, f"Tag nicht gefunden: {e}") from e

    tag_name = tag.get("name", "")
    if tag_name in ALL_PENDING_TAGS:
        raise HTTPException(400, f"Pipeline-Tag «{tag_name}» kann nicht gelöscht werden")

    try:
        pl_delete(f"/tags/{tag_id}/")
    except Exception as e:
        raise HTTPException(502, f"Paperless DELETE fehlgeschlagen: {e}") from e

    tags_data = _load_tags_json()
    tags_list = tags_data.get("tags", [])
    tags_data["tags"] = [
        t for t in tags_list
        if t.get("_paperless_id") != tag_id and t.get("name", "").lower() != tag_name.lower()
    ]
    _save_tags_json(tags_data)
    log.info("Tag gelöscht: %s (ID %s)", tag_name, tag_id)
    return {"status": "deleted", "id": tag_id, "name": tag_name}


@app.post("/api/paperless/document_types")
def api_create_doctype(body: dict = Body(...)):
    """Neuen Dokumenttyp in Paperless + document_types.json anlegen."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name fehlt")
    synonyme = body.get("synonyme", [])
    beschreibung = body.get("beschreibung", "")

    # Unique-Validierung gegen document_types.json
    dt_json_path = Path(os.environ.get("DOCUMENT_TYPES_JSON",
        "/opt/paperless-scripts/training/document_types.json"))
    if dt_json_path.exists():
        dt_data = json.loads(dt_json_path.read_text(encoding="utf-8"))
        all_strings = set()
        for t in dt_data.get("typen", []):
            all_strings.add(t["name"].lower())
            for s in t.get("synonyme", []):
                all_strings.add(s.lower())
        conflicts = [s for s in [name] + synonyme if s.lower() in all_strings]
        if conflicts:
            raise HTTPException(409, f"Bereits vergeben: {', '.join(conflicts)}")

    payload = {"name": name, "matching_algorithm": 0}
    payload.update(_default_permissions())
    try:
        result = pl_post("/document_types/", payload)
        pl_id = result.get("id")
        # Permissions via PATCH sicherstellen
        if pl_id:
            pl_patch(f"/document_types/{pl_id}/", _default_permissions())
    except Exception as e:
        raise HTTPException(502, str(e))

    # In document_types.json eintragen
    if dt_json_path.exists():
        dt_data = json.loads(dt_json_path.read_text(encoding="utf-8"))
        dt_data["typen"].append({
            "name": name,
            "synonyme": synonyme,
            "beschreibung": beschreibung,
            "_paperless_id": pl_id,
        })
        dt_json_path.write_text(json.dumps(dt_data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"status": "created", "name": name, "id": pl_id}


@app.get("/api/paperless/document_types", response_class=JSONResponse)
def api_doctypes():
    """Alle Dokumenttypen aus Paperless + Synonyme aus document_types.json."""
    try:
        result = pl_get("/document_types/", {"page_size": 200})
        pl_types = {t["name"].lower(): t for t in result.get("results", [])}

        # Synonyme aus document_types.json anreichern
        dt_json_path = Path(os.environ.get("DOCUMENT_TYPES_JSON",
            "/opt/paperless-scripts/training/document_types.json"))
        synonym_map = {}
        if dt_json_path.exists():
            dt_data = json.loads(dt_json_path.read_text(encoding="utf-8"))
            for t in dt_data.get("typen", []):
                synonym_map[t["name"].lower()] = {
                    "synonyme":      t.get("synonyme", []),
                    "beschreibung":  t.get("beschreibung", ""),
                    "ausschliessen": t.get("ausschliessen", []),
                    "fix_tags":      t.get("fix_tags", []),
                    "feldprofil":    t.get("feldprofil", {}),
                }

        enriched = []
        for t in result.get("results", []):
            entry = dict(t)
            sm = synonym_map.get(t["name"].lower(), {})
            entry["synonyme"]      = sm.get("synonyme", [])
            entry["beschreibung"]  = sm.get("beschreibung", "")
            entry["ausschliessen"] = sm.get("ausschliessen", [])
            entry["fix_tags"]      = sm.get("fix_tags", [])
            entry["feldprofil"]    = sm.get("feldprofil", {})
            enriched.append(entry)

        return {"results": enriched}
    except Exception as e:
        raise HTTPException(502, str(e))


@app.patch("/api/paperless/document_types/{dt_id}")
def api_patch_doctype(dt_id: int, body: dict = Body(...)):
    """Dokumenttyp-Synonyme und Beschreibung in document_types.json aktualisieren.
    Unique-Validierung: Name + Synonyme dürfen nirgendwo sonst vorkommen.
    """
    dt_json_path = Path(os.environ.get("DOCUMENT_TYPES_JSON",
        "/opt/paperless-scripts/training/document_types.json"))
    if not dt_json_path.exists():
        raise HTTPException(404, "document_types.json nicht gefunden")

    # Paperless-Name ermitteln
    try:
        pl_dt = pl_get(f"/document_types/{dt_id}/")
        dt_name = pl_dt.get("name", "")
    except Exception as e:
        raise HTTPException(502, str(e))

    dt_data = json.loads(dt_json_path.read_text(encoding="utf-8"))
    new_synonyme      = body.get("synonyme", [])
    new_beschreibung  = body.get("beschreibung", "")
    new_ausschliessen = body.get("ausschliessen", [])
    new_fix_tags      = body.get("fix_tags", [])
    new_feldprofil    = body.get("feldprofil", {})

    # Unique-Check: neue Synonyme dürfen nicht bei anderen Typen vorkommen
    all_strings = set()
    for t in dt_data.get("typen", []):
        if t["name"].lower() == dt_name.lower():
            continue
        all_strings.add(t["name"].lower())
        for s in t.get("synonyme", []):
            all_strings.add(s.lower())
    conflicts = [s for s in new_synonyme if s.lower() in all_strings]
    if conflicts:
        raise HTTPException(409, f"Synonym bereits vergeben: {', '.join(conflicts)}")

    found = False
    for t in dt_data.get("typen", []):
        if t["name"].lower() == dt_name.lower():
            t["synonyme"]      = new_synonyme
            t["beschreibung"]  = new_beschreibung
            t["ausschliessen"] = new_ausschliessen
            t["fix_tags"]      = new_fix_tags
            t["feldprofil"]    = new_feldprofil
            t["_paperless_id"] = dt_id
            found = True
            break
    if not found:
        dt_data.setdefault("typen", []).append({
            "name":          dt_name,
            "synonyme":      new_synonyme,
            "beschreibung":  new_beschreibung,
            "ausschliessen": new_ausschliessen,
            "fix_tags":      new_fix_tags,
            "feldprofil":    new_feldprofil,
            "_paperless_id": dt_id,
        })

    dt_json_path.write_text(json.dumps(dt_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "updated",
        "name": dt_name,
        "synonyme": new_synonyme,
        "fix_tags": new_fix_tags,
        "feldprofil": new_feldprofil,
    }


@app.patch("/api/correspondents/{entry_name:path}")
def api_patch_correspondent(entry_name: str, body: dict = Body(...)):
    """Korrespondenten-Eintrag in correspondents.json aktualisieren.
    Pflegbare Felder: varianten, match, default_dokumenttyp, typische_ordner,
                      notiz, fix_tags, verbotene_doctypen, verbotene_ordner, beziehungen.
    NICHT pflegbar via API: name (Primary Key), _paperless (intern).
    """
    corr_map = load_corr_map()
    entry = next((e for e in corr_map.get("eintraege", [])
                  if e["name"].lower() == entry_name.lower()), None)
    if not entry:
        raise HTTPException(404, f"Korrespondent '{entry_name}' nicht gefunden")

    # kuerzel Uniqueness prüfen
    if "kuerzel" in body:
        new_kuerzel = (body.get("kuerzel") or "").strip().upper()
        if new_kuerzel and not _check_kuerzel_unique(new_kuerzel, exclude_name=entry["name"], corr_map=corr_map):
            raise HTTPException(409, f"Kürzel '{new_kuerzel}' wird bereits von einem anderen Korrespondenten verwendet")

    allowed = [
        "varianten", "match", "default_dokumenttyp", "typische_ordner", "notiz",
        "fix_tags", "verbotene_doctypen", "verbotene_ordner", "verbotene_tags",
        "nicht_verwechseln_mit", "beziehungen", "kuerzel",
    ]
    for field in allowed:
        if field in body:
            val = body[field]
            if field == "kuerzel":
                val = (body[field] or "").strip().upper()
            elif field == "nicht_verwechseln_mit":
                val = _normalize_string_list(body[field])
            entry[field] = val

    for bez in entry.get("beziehungen", []):
        _normalize_beziehung_fields(bez)

    # Defaults setzen falls neu
    entry.setdefault("fix_tags", [])
    entry.setdefault("verbotene_doctypen", [])
    entry.setdefault("verbotene_ordner", [])
    entry.setdefault("verbotene_tags", [])
    entry.setdefault("nicht_verwechseln_mit", [])
    entry.setdefault("beziehungen", [])
    entry.setdefault("kuerzel", "")

    save_corr_map(corr_map)
    return {"status": "updated", "name": entry_name}



@app.get("/api/manifest", response_class=JSONResponse)
def api_manifest():
    """Manifest lesen."""
    manifest_path = Path(os.environ.get("MANIFEST_PATH",
        "/opt/paperless-scripts/training/manifest.json"))
    if not manifest_path.exists():
        raise HTTPException(404, "manifest.json nicht gefunden")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


@app.post("/api/manifest/ordner")
def api_manifest_create_ordner(body: dict = Body(...)):
    """Neuen Manifest-Ordner anlegen + Storage Path in Paperless erstellen.
    Format: Elternordner/Unterordner (z.B. Vorname/Sonstiges, Familie/Versicherung/Policen)
    """
    manifest_path = Path(os.environ.get("MANIFEST_PATH",
        "/opt/paperless-scripts/training/manifest.json"))
    pfad = (body.get("pfad") or "").strip()
    if not pfad:
        raise HTTPException(400, "pfad fehlt")
    # Format-Validierung: Buchstaben, Zahlen, /, Umlaute, Leerzeichen, -, _
    import re as _re
    if not _re.match(r'^[A-Za-zÄÖÜäöüß0-9/\s\-_]+$', pfad):
        raise HTTPException(400, "Ungültiges Pfad-Format. Erlaubt: Buchstaben, Zahlen, /, -, _ (z.B. Person/Kategorie)")
    if pfad.startswith("/") or pfad.endswith("/") or "//" in pfad:
        raise HTTPException(400, "Pfad darf nicht mit / beginnen/enden oder // enthalten")

    if not manifest_path.exists():
        raise HTTPException(404, "manifest.json nicht gefunden")

    import fcntl as _fcntl
    fd = open(manifest_path, "r+", encoding="utf-8")
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        data = json.load(fd)
        # Prüfen ob Pfad bereits existiert
        existing = next((e for e in data.get("ordner", []) if e.get("pfad") == pfad), None)
        if existing:
            raise HTTPException(409, f"Ordner '{pfad}' existiert bereits im Manifest")

        new_entry = {
            "pfad": pfad,
            "beschreibung": body.get("beschreibung", ""),
            "abgrenzung": "",
            "erlaubte_tags": body.get("erlaubte_tags", []),
            "verbotene_tags": [],
            "erlaubte_dokumenttypen": body.get("erlaubte_dokumenttypen", []),
            "max_tags": body.get("max_tags", 4),
            "pending": True,
        }
        data.setdefault("ordner", []).append(new_entry)
        fd.seek(0); fd.truncate()
        json.dump(data, fd, ensure_ascii=False, indent=2)
        fd.flush()
    finally:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        fd.close()

    # Storage Path in Paperless anlegen
    _ensure_storage_path(pfad)

    log.info("Manifest: Neuer Ordner '%s' angelegt", pfad)
    return {"status": "created", "pfad": pfad}


@app.patch("/api/manifest/ordner/{pfad:path}")
def api_manifest_patch_ordner(pfad: str, body: dict = Body(...)):
    """Manifest-Ordner-Eintrag patchen (erlaubte_tags, max_tags etc.)."""
    manifest_path = Path(os.environ.get("MANIFEST_PATH",
        "/opt/paperless-scripts/training/manifest.json"))
    if not manifest_path.exists():
        raise HTTPException(404, "manifest.json nicht gefunden")
    import fcntl as _fcntl
    fd = open(manifest_path, "r+", encoding="utf-8")
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        data = json.load(fd)
        entry = next((e for e in data.get("ordner", []) if e.get("pfad") == pfad), None)
        if not entry:
            raise HTTPException(404, f"Ordner '{pfad}' nicht im Manifest")
        allowed_fields = ["erlaubte_tags", "verbotene_tags", "erlaubte_dokumenttypen",
                          "max_tags", "beschreibung", "abgrenzung", "pending"]
        for f in allowed_fields:
            if f in body:
                val = body[f]
                # Listen deduplizieren (Reihenfolge erhalten)
                if isinstance(val, list):
                    seen = set()
                    deduped = []
                    for item in val:
                        if isinstance(item, str) and item.lower() not in seen:
                            deduped.append(item)
                            seen.add(item.lower())
                    val = deduped
                entry[f] = val
        fd.seek(0); fd.truncate()
        json.dump(data, fd, ensure_ascii=False, indent=2)
        fd.flush()
        return {"status": "updated", "pfad": pfad}
    finally:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        fd.close()


_DEFAULT_FZ_KATEGORIEN = ["auto", "mofa", "moped"]


def _normalize_fahrzeug_kategorien(raw) -> list[str]:
    """Kleinbuchstaben, eindeutig, Reihenfolge beibehalten."""
    if not raw:
        return list(_DEFAULT_FZ_KATEGORIEN)
    seen: set[str] = set()
    out: list[str] = []
    for k in raw:
        s = str(k).strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out or list(_DEFAULT_FZ_KATEGORIEN)


@app.get("/api/family", response_class=JSONResponse)
def api_family():
    """Haushaltskonfiguration lesen (family.json)."""
    if not FAMILY_JSON.exists():
        return {"version": "1.0", "haushalt": {"name": "", "land": "CH", "sprache": "de"},
                "personen": [], "fahrzeuge": [], "fahrzeug_kategorien": list(_DEFAULT_FZ_KATEGORIEN)}
    data = json.loads(FAMILY_JSON.read_text(encoding="utf-8"))
    data["fahrzeug_kategorien"] = _normalize_fahrzeug_kategorien(data.get("fahrzeug_kategorien"))
    return data


@app.patch("/api/family")
def api_patch_family(body: dict = Body(...)):
    """Haushaltskonfiguration speichern (family.json).
    Schreibt haushalt, personen, fahrzeuge, beziehungen atomar zurück.
    """
    allowed = {"haushalt", "personen", "fahrzeuge", "fahrzeug_kategorien", "beziehungen"}
    if not set(body.keys()) <= allowed:
        raise HTTPException(400, f"Unbekannte Felder: {set(body.keys()) - allowed}")

    data = {}
    if FAMILY_JSON.exists():
        data = json.loads(FAMILY_JSON.read_text(encoding="utf-8"))

    for key in allowed:
        if key in body:
            data[key] = body[key]

    data.setdefault("version", "1.0")
    data["fahrzeug_kategorien"] = _normalize_fahrzeug_kategorien(data.get("fahrzeug_kategorien"))

    if "fahrzeug_kategorien" in body and not data["fahrzeug_kategorien"]:
        raise HTTPException(400, "Mindestens eine Kategorie erforderlich")

    # Validierung: Personen (AHV, Geburtsdatum)
    if "personen" in body:
        import re as _re
        _ahv_re = _re.compile(r"^756\.\d{4}\.\d{4}\.\d{2}$")
        for p in data.get("personen", []):
            ahv = (p.get("ahv_nummer") or "").strip()
            if ahv:
                digits = _re.sub(r"\D", "", ahv)
                if len(digits) != 13 or not digits.startswith("756"):
                    raise HTTPException(400, f"AHV «{ahv}» ungültig — Format: 756.XXXX.XXXX.XX")
                p["ahv_nummer"] = f"{digits[0:3]}.{digits[3:7]}.{digits[7:11]}.{digits[11:13]}"
            geb = (p.get("geburtsdatum") or "").strip()
            if geb:
                if not _parse_geburtsdatum(geb):
                    raise HTTPException(400, f"Geburtsdatum «{geb}» ungültig — z.B. 15.5.1980 oder 15.5.80")
                p["geburtsdatum"] = _normalize_geburtsdatum(geb)
            nv = p.get("namen_varianten")
            if nv is not None:
                if not isinstance(nv, list):
                    raise HTTPException(400, "namen_varianten muss eine Liste sein")
                p["namen_varianten"] = [str(v).strip() for v in nv if str(v).strip()]

    # Validierung: Kennzeichen müssen unique sein
    if "fahrzeuge" in body:
        kennzeichen_list = [f.get("kennzeichen", "").replace(" ", "").upper()
                            for f in data.get("fahrzeuge", [])]
        if len(kennzeichen_list) != len(set(k for k in kennzeichen_list if k)):
            raise HTTPException(409, "Kennzeichen müssen eindeutig sein")
        person_ids = {p["id"] for p in data.get("personen", []) if "id" in p}
        valid_fz_typen = set(data["fahrzeug_kategorien"])
        default_typ = data["fahrzeug_kategorien"][0]
        for fz in data.get("fahrzeuge", []):
            if fz.get("person_id") and fz["person_id"] not in person_ids:
                raise HTTPException(409, f"person_id '{fz['person_id']}' nicht in personen definiert — zuerst Personen speichern")
            typ = (fz.get("typ") or default_typ).strip().lower()
            fz["typ"] = typ
            if typ and typ not in valid_fz_typen:
                erlaubt = ", ".join(data["fahrzeug_kategorien"])
                raise HTTPException(400, f"Unbekannte Kategorie '{typ}' — erlaubt: {erlaubt}")
            routing = fz.get("routing_ordner")
            if routing is None:
                routing = bool((fz.get("ordner") or "").strip())
                fz["routing_ordner"] = routing
            if routing and not (fz.get("ordner") or "").strip():
                raise HTTPException(400, f"Ziel-Ordner fehlt bei Kennzeichen «{fz.get('kennzeichen', '?')}» (routing_ordner aktiv)")
            if not routing:
                fz["ordner"] = ""
            fz["default_tag"] = (fz.get("default_tag") or "").strip()

    # Validierung: Beziehungen
    if "beziehungen" in body:
        valid_typen = {"arbeitgeber", "bank", "krankenkasse", "arzt", "versicherung", "sonstiges"}
        for bez in data.get("beziehungen", []):
            if bez.get("typ") and bez["typ"] not in valid_typen:
                raise HTTPException(400, f"Unbekannter Beziehungstyp: {bez['typ']}")
            if not bez.get("korrespondent", "").strip():
                raise HTTPException(400, "Korrespondent darf nicht leer sein")
            if not bez.get("ordner", "").strip():
                raise HTTPException(400, f"Ziel-Ordner fehlt bei «{bez.get('korrespondent')}»")

    FAMILY_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = FAMILY_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(FAMILY_JSON)

    log.info("family.json gespeichert: %d Personen, %d Fahrzeuge, %d Beziehungen",
             len(data.get("personen", [])), len(data.get("fahrzeuge", [])),
             len(data.get("beziehungen", [])))
    return {"status": "updated"}


@app.post("/api/regex-assistent")
def api_regex_assistent(body: dict = Body(...)):
    """
    Regex-Assistent: aus Beispiel-String einen Regex ableiten via Ollama.
    Verwendet OLLAMA_REGEX_MODEL (default llama3.3:70b) — NICHT das Vision-Modell.
    body: {beispiel: "LV_889.117", feldname: "Policennummer", kontext: "Zürich Versicherung",
           weitere_beispiele: ["LV_123.456"]}
    """
    beispiel  = (body.get("beispiel") or "").strip()
    feldname  = (body.get("feldname") or "Wert").strip()
    kontext   = (body.get("kontext") or "").strip()
    weitere   = body.get("weitere_beispiele") or []

    if not beispiel:
        raise HTTPException(400, "beispiel fehlt")

    # Dediziertes Modell für Regex — unabhängig vom Vision-Modell
    ollama_url   = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model = os.environ.get("OLLAMA_REGEX_MODEL",
                   os.environ.get("OLLAMA_MODEL", "llama3.3:70b"))

    alle_beispiele = [beispiel] + [w for w in weitere if w]
    beispiele_str  = "\n".join(f"  - {b}" for b in alle_beispiele)
    kontext_str    = f"\nKontext (Absender): {kontext}" if kontext else ""
    fg_name        = feldname.lower().replace(" ", "_")

    prompt = (
        "Du bist ein Regex-Experte fuer deutschsprachige Dokumente.\n\n"
        "Aufgabe: Erstelle einen Python-Regex, der das FORMAT des Feldes '" + feldname + "' erkennt.\n\n"
        "Beispielwerte (alle sollen matchen):\n" + beispiele_str + kontext_str + "\n\n"
        "Wichtige Regeln:\n"
        "- Erkenne das MUSTER/FORMAT, nicht den exakten Wert\n"
        "- Beispiel: '70.735.634' hat Format NN.NNN.NNN → Regex: \\d{2}\\.\\d{3}\\.\\d{3}\n"
        "- Beispiel: 'LV_889.117' hat Format LV_NNN.NNN → Regex: LV_\\d{3}\\.\\d{3}\n"
        "- Verwende Named Group: (?P<" + fg_name + ">MUSTER)\n"
        "- Kein re.IGNORECASE noetig ausser Buchstaben im Muster variieren\n"
        "- Antworte NUR mit JSON, kein Markdown, keine Erklaerung ausserhalb:\n"
        '{"regex": "(?P<' + fg_name + '>...)", "erklaerung": "1 Satz was das Muster beschreibt", '
        '"test_matches": ["Wert1", "Wert2"], "test_no_matches": ["FalscherWert"]}'
    )

    try:
        import urllib.request as _ur
        payload = json.dumps({
            "model": ollama_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.05, "num_predict": 256}
        }).encode()
        req = _ur.Request(
            f"{ollama_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with _ur.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
        raw = result.get("response", "{}")
        import re as _re
        json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
            return {"status": "ok", "model_used": ollama_model, **parsed}
        return {"status": "ok", "model_used": ollama_model, "regex": raw.strip(),
                "erklaerung": "", "test_matches": [], "test_no_matches": []}
    except Exception as e:
        log.error("Regex-Assistent Ollama-Fehler: %s", e)
        raise HTTPException(502, f"Ollama nicht erreichbar: {e}")

@app.get("/api/korr-typen", response_class=JSONResponse)
def api_korr_typen():
    """Alle verwendeten Korrespondenten-Typen aus correspondents.json."""
    corr_map = load_corr_map()
    typen = sorted(set(
        e.get("typ", "") for e in corr_map.get("eintraege", []) if e.get("typ")
    ))
    # Defaults hinzufügen falls noch keine Einträge
    defaults = ["Behörde","Bank","Versicherung","Arzt","Arbeitgeber","Kundenservice",
                "Verein","Online-Shop","Händler","Handwerk","Telekommunikation",
                "Energie","Transport","Sonstiges"]
    all_typen = sorted(set(typen + defaults))
    return {"typen": all_typen}


# ══════════════════════════════════════════════
# HTML UI (Single-Page mit Vanilla JS + Fetch)
# ══════════════════════════════════════════════

# HTML UI wird aus separater Datei geladen (beim Start gecacht — Deploy = Service-Restart)
_UI_FILE = Path(__file__).parent / "paper_manager_ui.html"
_UI_FALLBACK = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>paper.manager</title>
<style>body{background:#0f1117;color:#e2e8f0;font-family:sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{text-align:center;padding:40px;border:1px solid #334155;border-radius:8px}
h2{color:#f87171}code{background:#1e293b;padding:4px 8px;border-radius:4px}
</style></head><body><div class="box">
<h2>paper_manager_ui.html nicht gefunden</h2>
<p>Datei fehlt auf dem Server:</p>
<code>/opt/paperless-scripts/paper_manager_ui.html</code>
<p>Bitte deployen und Service neu starten.</p>
</div></body></html>"""


def _load_ui_html() -> str:
    if _UI_FILE.exists():
        return _UI_FILE.read_text(encoding="utf-8")
    return _UI_FALLBACK


_UI_HTML = _load_ui_html()  # Fallback; Live-Route lädt Datei bei jedem Request


@app.get("/api/proxy/document/{doc_id}/preview/")
def proxy_document_preview(doc_id: int):
    """Proxied PDF-Vorschau — funktioniert auch per IP ohne Authentik-Cookie."""
    try:
        r = requests.get(
            f"{PAPERLESS_API_URL.rstrip('/')}/documents/{doc_id}/preview/",
            headers=PAPERLESS_HEADERS,
            stream=True,
            timeout=30,
        )
        if r.status_code in (301, 302, 303, 307, 308):
            raise HTTPException(401, "Paperless: Authentifizierung fehlgeschlagen (Redirect)")
        if not r.ok:
            raise HTTPException(r.status_code, f"Paperless: {r.text[:200]}")
        ct = r.headers.get("content-type", "application/pdf")
        if "text/html" in ct:
            raise HTTPException(401, "Paperless: Login-Seite erhalten statt PDF")
        return StreamingResponse(
            r.iter_content(chunk_size=65536),
            media_type=ct,
            headers={"Content-Disposition": f"inline; filename=document_{doc_id}.pdf"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Proxy-Fehler: {e}")


@app.get("/api/proxy/document/{doc_id}/thumb/")
def proxy_document_thumb(doc_id: int):
    """Proxied Thumbnail — für Vorschau-Bild im Dokument-Review."""
    try:
        r = requests.get(
            f"{PAPERLESS_API_URL.rstrip('/')}/documents/{doc_id}/thumb/",
            headers=PAPERLESS_HEADERS,
            stream=True,
            timeout=15,
        )
        if r.status_code in (301, 302, 303, 307, 308):
            raise HTTPException(401, "Paperless: Authentifizierung fehlgeschlagen (Redirect)")
        if not r.ok:
            raise HTTPException(r.status_code, f"Paperless: {r.text[:200]}")
        ct = r.headers.get("content-type", "image/webp")
        if "text/html" in ct:
            raise HTTPException(401, "Paperless: Login-Seite erhalten statt Bild")
        return StreamingResponse(
            r.iter_content(chunk_size=32768),
            media_type=ct,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Proxy-Fehler: {e}")


@app.get("/", response_class=HTMLResponse)
async def root():
    """HTML UI — bei jedem Request frisch von Disk (Deploy ohne Service-Restart sichtbar)."""
    return HTMLResponse(content=_load_ui_html())
