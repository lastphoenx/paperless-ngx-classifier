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

__version__ = "2.7"  # 2.7: kuerzel Feld (unique), Uniqueness-Validierung in PUT+PATCH+POST + Regex-Fix v2.6
import fcntl
from contextlib import contextmanager
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel

# ──────────────────────────────────────────────
# Konfiguration
# ──────────────────────────────────────────────
PAPERLESS_API_URL   = os.environ.get("PAPERLESS_API_URL", "http://localhost:8000/api")
PAPERLESS_API_TOKEN = os.environ.get("PAPERLESS_API_TOKEN", "")
CORRESPONDENTS_JSON = os.environ.get("CORRESPONDENTS_JSON", "data/correspondents.json")
PENDING_JSONL       = os.environ.get("PENDING_JSONL", "data/pending_correspondents.jsonl")
TAGS_JSON           = Path(os.environ.get("TAGS_JSON", "/opt/paperless-scripts/training/tags.json"))
FAMILY_JSON         = Path(os.environ.get("FAMILY_JSON", "/opt/paperless-scripts/training/family.json"))
PAPERLESS_VIEW_GROUPS   = [g.strip() for g in os.environ.get("PAPERLESS_VIEW_GROUPS", "family,Eltern").split(",")]
PAPERLESS_CHANGE_GROUPS = [g.strip() for g in os.environ.get("PAPERLESS_CHANGE_GROUPS", "Eltern").split(",")]
PENDING_REVIEW_TAG       = os.environ.get("PENDING_REVIEW_TAG",       "pending_review")
PENDING_QS_TAG           = os.environ.get("PENDING_QS_TAG",           "pending_qs")
PENDING_NEW_CORR_TAG     = os.environ.get("PENDING_NEW_CORR_TAG",      "pending_new_correspondent")
ALL_PENDING_TAGS         = {PENDING_REVIEW_TAG, PENDING_QS_TAG, PENDING_NEW_CORR_TAG}

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


@app.middleware("http")
async def require_paperless_session(request: Request, call_next):
    """Prüft ob eine gültige Paperless-Session vorhanden ist.
    API-Calls (/api/*): Token-Check via PAPER_MANAGER_TOKEN falls gesetzt,
    sonst Session-Cookie prüfen.
    Browser-Requests: kein Cookie → immer Redirect zu Paperless Login.
    """
    path = request.url.path
    paperless_url = os.environ.get("PAPERLESS_URL", "http://localhost:8000")
    # Für Browser-Login immer die INTERNE URL verwenden —
    # PAPERLESS_INTERNAL_URL zeigt direkt auf den Container ohne nginx/Authentik.
    # Wer lokal (IP) zugreift, soll beim lokalen Paperless-Login landen.
    # Wer extern (Domain) zugreift, ist bereits via Authentik authentifiziert.
    paperless_internal = os.environ.get("PAPERLESS_INTERNAL_URL",
                         os.environ.get("PAPERLESS_URL", "http://localhost:8000"))

    # API-Calls: Token oder Session prüfen
    if path.startswith("/api/"):
        # Token-Auth (direkte API-Zugriffe ohne Browser)
        if _INTERNAL_TOKEN:
            token = (
                request.headers.get("X-Paper-Manager-Token") or
                request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            )
            if token == _INTERNAL_TOKEN:
                return await call_next(request)
        # Session-Cookie-Auth (Browser-Flow)
        session_cookie = request.cookies.get("sessionid")
        if session_cookie:
            import urllib.request as _ur
            try:
                req = _ur.Request(
                    f"{paperless_url}/api/profile/",
                    headers={"Cookie": f"sessionid={session_cookie}"}
                )
                with _ur.urlopen(req, timeout=3) as r:
                    if r.status == 200:
                        return await call_next(request)
            except Exception:
                pass
        # Kein gültiger Token und keine gültige Session → 401
        from fastapi.responses import JSONResponse as _JR
        return _JR(status_code=401, content={"detail": "Nicht authentifiziert"})

    # Browser-Requests: kein Cookie → Login-Redirect
    session_cookie = request.cookies.get("sessionid")
    if session_cookie:
        import urllib.request as _ur
        try:
            req = _ur.Request(
                f"{paperless_url}/api/profile/",
                headers={"Cookie": f"sessionid={session_cookie}"}
            )
            with _ur.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    return await call_next(request)
        except Exception:
            pass

    # Login-Redirect: gleiche IP/Domain wie Request-Host, aber Port 8000 (Paperless)
    # Beispiel: Request kommt von 192.168.131.31:8100 → Login auf 192.168.131.31:8000
    # Beispiel: Request kommt von paperless.santinel.li → Login auf paperless.santinel.li (Authentik)
    host = request.headers.get("host", "localhost:8100")
    proto = request.headers.get("x-forwarded-proto", "http")
    host_without_port = host.split(":")[0]

    # Paperless-Port: intern 8000, extern via nginx (kein Port)
    is_ip = host_without_port.replace(".", "").isdigit()
    if is_ip:
        # Direkte IP-Eingabe → Paperless auf selber IP Port 8000
        paperless_login_base = f"http://{host_without_port}:8000"
    else:
        # Domain → externe URL (Authentik davor)
        paperless_login_base = f"{proto}://{host_without_port}"

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
    merge_ziel_name:         Optional[str]       = None
    reviewed_by:             Optional[str]       = "admin"
    extraktion_muster:       Optional[dict]      = None   # {feldname: ExtraktionsMuster}
    erwartungen:             Optional[dict]      = None   # {hat_qr_rechnung: bool, ...}


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
    for e in corr_map.get("eintraege", []):
        if e["name"] == (exclude_name or ""):
            continue
        all_names.add(e["name"].lower())
        for m in e.get("match", []):
            all_matches[m.lower()] = e["name"]
        for v in e.get("varianten", []):
            all_varianten[v.lower()] = e["name"]
    return {
        "names":     all_names,
        "matches":   all_matches,
        "varianten": all_varianten,
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

    return errors, warnings


def load_corr_map() -> dict:
    path = Path(CORRESPONDENTS_JSON)
    if not path.exists():
        return {"version": "1.0", "eintraege": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def remove_all_pending_tags(doc_ids: list[int]) -> None:
    """ALLE pending-Tags entfernen beim Freigeben.
    Entfernt: pending_review, pending_qs, pending_new_correspondent
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


def _ensure_storage_path(pfad: str) -> None:
    """Storage Path in Paperless anlegen falls nicht vorhanden."""
    try:
        existing = pl_get("/storage_paths/", {"name__iexact": pfad})
        if existing.get("count", 0) > 0:
            return
        template = pfad + "/{created_year}/{correspondent}/{title}"
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
        result = pl_post("/storage_paths/", {"name": pfad, "path": pfad})
        return result.get("id")
    except Exception as ex:
        log.warning("Storage Path '%s' nicht gefunden/angelegt: %s", pfad, ex)
        return None


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

    # 1. Vollständige Validation VOR Paperless-Anlage
    corr_map = load_corr_map()
    errors, warnings = _validate_correspondent_entry(
        name=name,
        match_strings=match_list,
        varianten=varianten,
        typische_ordner=ordner,
        corr_map=corr_map,
    )
    if errors:
        raise HTTPException(409, f"Validation fehlgeschlagen: {'; '.join(errors)}")
    if warnings:
        log.warning("Korrespondent '%s' — Warnings: %s", name, "; ".join(warnings))

    # 2. Paperless: existiert schon?
    paperless_id = get_correspondent_id_by_name(name)
    if paperless_id:
        log.info("approve_neu: '%s' existiert bereits in Paperless (ID %s) — kein Duplikat angelegt", name, paperless_id)
    else:
        paperless_id = create_correspondent(name, match_list)

    # 3. Dokumente zuweisen + Tag entfernen + Dokumenttyp setzen
    doc_ids = entry.get("source_document_ids", [])
    assign_documents_to_correspondent(doc_ids, paperless_id)
    remove_all_pending_tags(doc_ids)

    # Dokumenttyp-ID ermitteln/anlegen falls nur Name vorhanden
    if default_dt and not default_dt_id:
        default_dt_id = _resolve_or_create_doctype_id(default_dt)

    # Dokumenttyp auf Dokumente setzen
    if default_dt_id and doc_ids:
        _set_document_type_on_documents_by_id(doc_ids, default_dt_id)
    elif default_dt and doc_ids:
        _set_document_type_on_documents(doc_ids, default_dt)

    # Audit-Log: fehlende Klassifizierung nachholen (Tags, Storage Path, Custom Fields)
    for doc_id in doc_ids:
        _apply_audit_classification(doc_id)

    # 4. In correspondents.json eintragen
    new_entry = {
        "name": name,
        "varianten": varianten,
        "match": match_list,
        "matching_algorithm": "any",
        "default_dokumenttyp":    default_dt,
        "default_dokumenttyp_id": default_dt_id,
        "typische_ordner": ordner,
        "notiz": notiz,
        "extraktion_muster": extr_muster or {},
        "erwartungen": erwartungen or {},
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

    # 5. Manifest-Einträge für neue Ordner als "pending" anlegen
    _ensure_manifest_entries(ordner, name)

    return f"Korrespondent '{name}' angelegt (Paperless-ID {paperless_id}), {len(doc_ids)} Dokumente zugewiesen"


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


def load_document_review_queue() -> list[dict]:
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
    return entries


def save_document_review_queue(entries: list[dict]) -> None:
    DOCUMENT_REVIEW_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with open(DOCUMENT_REVIEW_QUEUE, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


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


@app.get("/api/correspondents", response_class=JSONResponse)
def api_correspondents():
    """Kanonisierungs-Map als JSON."""
    return load_corr_map()


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
                value    = cf.get("value")
                if field_id in cf_options and value:
                    value = cf_options[field_id].get(value, value)
                resolved.append({"field": field_id, "value": value})
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
            entries[original_index]["status"] = "rejected"
            entries[original_index]["reviewed_by"] = decision.reviewed_by
            entries[original_index]["reviewed_at"] = now
            save_pending(entries)
            return {"status": "rejected", "message": "Eintrag abgelehnt"}

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
                  "beziehungen", "kuerzel"]:
        if field in body:
            entry[field] = body[field] if field != "kuerzel" else (body[field] or "").strip().upper()

    # Defaults setzen falls neu
    entry.setdefault("fix_tags", [])
    entry.setdefault("verbotene_doctypen", [])
    entry.setdefault("verbotene_ordner", [])
    entry.setdefault("verbotene_tags", [])
    entry.setdefault("beziehungen", [])
    entry.setdefault("kuerzel", "")

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
def api_config():
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
    return {
        "paperless_url": os.environ.get("PAPERLESS_URL", "http://localhost:8000"),
        "pending_mode":  _get_pending_mode(),
        "versions": {
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
                # Manuelle Neuklassifizierung: ordner und/oder tags aus Body
                if body.get("storage_path_id"):
                    patch["storage_path"] = body["storage_path_id"]
                if body.get("tag_ids"):
                    # Bestehende Tags lesen + mergen
                    try:
                        doc = pl_get(f"/documents/{doc_id}/")
                        existing = doc.get("tags", [])
                        patch["tags"] = list(dict.fromkeys(existing + body["tag_ids"]))
                    except Exception:
                        patch["tags"] = body["tag_ids"]
                if body.get("correspondent_id"):
                    patch["correspondent"] = body["correspondent_id"]
                if body.get("document_type_id"):
                    patch["document_type"] = body["document_type_id"]

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
                    "synonyme":     t.get("synonyme", []),
                    "beschreibung": t.get("beschreibung", ""),
                    "ausschliessen": t.get("ausschliessen", []),
                }

        enriched = []
        for t in result.get("results", []):
            entry = dict(t)
            sm = synonym_map.get(t["name"].lower(), {})
            entry["synonyme"]      = sm.get("synonyme", [])
            entry["beschreibung"]  = sm.get("beschreibung", "")
            entry["ausschliessen"] = sm.get("ausschliessen", [])
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
            "_paperless_id": dt_id,
        })

    dt_json_path.write_text(json.dumps(dt_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "updated", "name": dt_name, "synonyme": new_synonyme, "fix_tags": new_fix_tags}


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
        "beziehungen", "kuerzel",
    ]
    for field in allowed:
        if field in body:
            entry[field] = body[field] if field != "kuerzel" else (body[field] or "").strip().upper()

    # Defaults setzen falls neu
    entry.setdefault("fix_tags", [])
    entry.setdefault("verbotene_doctypen", [])
    entry.setdefault("verbotene_ordner", [])
    entry.setdefault("verbotene_tags", [])
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
    # Format-Validierung: nur Buchstaben, Zahlen, /, Umlaute, Leerzeichen, -
    import re as _re
    if not _re.match(r'^[A-Za-zÄÖÜäöüß0-9/\s\-]+$', pfad):
        raise HTTPException(400, "Ungültiges Pfad-Format. Erlaubt: Buchstaben, Zahlen, /, - (z.B. Person/Kategorie)")
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


@app.get("/api/family", response_class=JSONResponse)
def api_family():
    """Haushaltskonfiguration lesen (family.json)."""
    if not FAMILY_JSON.exists():
        return {"version": "1.0", "haushalt": {"name": "", "land": "CH", "sprache": "de"},
                "personen": [], "fahrzeuge": []}
    return json.loads(FAMILY_JSON.read_text(encoding="utf-8"))


@app.patch("/api/family")
def api_patch_family(body: dict = Body(...)):
    """Haushaltskonfiguration speichern (family.json).
    Schreibt haushalt, personen, fahrzeuge, beziehungen atomar zurück.
    """
    allowed = {"haushalt", "personen", "fahrzeuge", "beziehungen"}
    if not set(body.keys()) <= allowed:
        raise HTTPException(400, f"Unbekannte Felder: {set(body.keys()) - allowed}")

    data = {}
    if FAMILY_JSON.exists():
        data = json.loads(FAMILY_JSON.read_text(encoding="utf-8"))

    for key in allowed:
        if key in body:
            data[key] = body[key]

    data.setdefault("version", "1.0")

    # Validierung: Kennzeichen müssen unique sein
    if "fahrzeuge" in body:
        kennzeichen_list = [f.get("kennzeichen", "").replace(" ", "").upper()
                            for f in data.get("fahrzeuge", [])]
        if len(kennzeichen_list) != len(set(k for k in kennzeichen_list if k)):
            raise HTTPException(409, "Kennzeichen müssen eindeutig sein")
        person_ids = {p["id"] for p in data.get("personen", []) if "id" in p}
        for fz in data.get("fahrzeuge", []):
            if fz.get("person_id") and fz["person_id"] not in person_ids:
                raise HTTPException(409, f"person_id '{fz['person_id']}' nicht in personen definiert — zuerst Personen speichern")

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

# HTML UI wird aus separater Datei geladen
_UI_FILE = Path(__file__).parent / "paper_manager_ui.html"


@app.get("/", response_class=HTMLResponse)
def root():
    """HTML UI laden — per-request damit Deploy ohne Restart wirkt."""
    if _UI_FILE.exists():
        return HTMLResponse(content=_UI_FILE.read_text(encoding="utf-8"))
    # Fallback: eingebettetes Minimal-UI
    return HTMLResponse(content="""<!DOCTYPE html>
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
</div></body></html>""")
