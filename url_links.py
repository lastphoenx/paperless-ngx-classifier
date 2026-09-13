"""
Zentrale URL-Bausteine für paper.manager.

Eine Quelle für /api/config → Frontend (PmLinks).
Regeln:
  - Dokument-PDF im iframe / eingebettete Vorschau → paper.manager-Proxy (gleiche Origin)
  - Dokument-PDF / Details in neuem Tab → Paperless (:8000 oder Domain)
  - paper.manager Start → :8100 bzw. /corr-manager/
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from starlette.requests import Request


def effective_paperless_url(request: Request | None = None) -> str:
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


def paperless_internal_base() -> str:
    return os.environ.get(
        "PAPERLESS_INTERNAL_URL",
        os.environ.get("PAPERLESS_URL", "http://localhost:8000"),
    ).rstrip("/")


def paperless_public_base() -> str:
    return os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")


def manager_internal_url() -> str | None:
    explicit = os.environ.get("PAPER_MANAGER_INTERNAL_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    parsed = urlparse(paperless_internal_base())
    if parsed.hostname and parsed.hostname.replace(".", "").isdigit():
        return f"http://{parsed.hostname}:8100"
    return None


def manager_public_url() -> str | None:
    pub = os.environ.get("PAPER_MANAGER_PUBLIC_URL", "").strip().rstrip("/")
    return pub or None


def request_api_prefix(request: Request) -> str:
    """Relatives API-Präfix wie im Frontend (API_BASE)."""
    fwd = request.headers.get("x-forwarded-prefix", "").strip("/")
    if fwd == "corr-manager" or request.url.path.startswith("/corr-manager"):
        return "/corr-manager/api"
    return "/api"


def request_manager_origin(request: Request) -> str:
    """Origin der paper.manager-Oberfläche (für absolute Proxy-URLs)."""
    host = request.headers.get("host", "localhost:8100")
    proto = request.headers.get("x-forwarded-proto", "http")
    fwd = request.headers.get("x-forwarded-prefix", "").strip("/")
    origin = f"{proto}://{host}"
    if fwd:
        origin = f"{origin}/{fwd}"
    return origin.rstrip("/")


def manager_urls_config() -> dict[str, str]:
    out: dict[str, str] = {}
    internal = manager_internal_url()
    public = manager_public_url()
    if internal:
        out["internal"] = internal
        out["home_internal"] = f"{internal}/#home"
    if public:
        out["public"] = f"{public}/"
        out["home_public"] = f"{public}/#home"
    return out


def document_urls(doc_id: int, request: Request) -> dict[str, Any]:
    """Alle Link-Varianten für ein Paperless-Dokument."""
    pl_current = effective_paperless_url(request).rstrip("/")
    pl_ip = paperless_internal_base()
    pl_pub = paperless_public_base()
    api_prefix = request_api_prefix(request)
    mgr_origin = request_manager_origin(request)
    proxy_path = f"{api_prefix}/proxy/document/{doc_id}/preview/"
    thumb_path = f"{api_prefix}/proxy/document/{doc_id}/thumb/"

    return {
        "doc_id": doc_id,
        "details": {
            "current": f"{pl_current}/documents/{doc_id}/details",
            "ip": f"{pl_ip}/documents/{doc_id}/details",
            "public": f"{pl_pub}/documents/{doc_id}/details",
        },
        "pdf_external": {
            "current": f"{pl_current}/api/documents/{doc_id}/preview/",
            "ip": f"{pl_ip}/api/documents/{doc_id}/preview/",
            "public": f"{pl_pub}/api/documents/{doc_id}/preview/",
        },
        "pdf_embed": f"{mgr_origin}{proxy_path}",
        "pdf_embed_path": proxy_path,
        "thumb_embed": f"{mgr_origin}{thumb_path}",
        "thumb_embed_path": thumb_path,
    }


def build_config_links(request: Request, handbuch_doc_id: int | None = None) -> dict[str, Any]:
    """Komplettes links-Objekt für /api/config."""
    links: dict[str, Any] = {
        "api_prefix": request_api_prefix(request),
        "manager_origin": request_manager_origin(request),
        "paperless": {
            "current": effective_paperless_url(request),
            "ip": paperless_internal_base(),
            "public": paperless_public_base(),
        },
        "manager": manager_urls_config(),
    }
    if handbuch_doc_id is not None:
        links["handbuch"] = document_urls(handbuch_doc_id, request)
    return links
