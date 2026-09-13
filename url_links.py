"""
Zentrale URL-Bausteine für paper.manager.

Nur bestehende .env-Keys:
  PAPERLESS_URL              → Domain (Browser, öffentlich)
  PAPERLESS_INTERNAL_URL     → Server→Paperless (Session-Check, API-Fallback)
  PAPERLESS_API_URL          → Server→Paperless API

Keine zusätzlichen PAPER_MANAGER_*-URL-Variablen.

Regeln:
  - iframe / eingebettete Vorschau → paper.manager-Proxy (gleiche Origin, :8100)
  - PDF / Details in neuem Tab → Paperless (Domain oder Request-IP :8000)
  - manager Domain → {PAPERLESS_URL}/corr-manager/
  - manager IP → http://{Request-Host-IP}:8100 (nur wenn Zugriff per IP)
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from starlette.requests import Request


def _server_lan_ip() -> str | None:
    """LAN-IP des Hosts (CT121) — für stabile IP-Links auch bei Domain-Zugriff."""
    try:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return None


def _request_host_ip(request: Request) -> str | None:
    host = (request.headers.get("host") or "").split(":")[0]
    if host.replace(".", "").isdigit():
        return host
    return None


def effective_paperless_url(request: Request | None = None) -> str:
    """Request-host-aware Paperless-URL für Links und Login-Redirect."""
    canonical = os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")
    if request is None:
        return canonical
    ip = _request_host_ip(request)
    if ip:
        return f"http://{ip}:8000"
    host = request.headers.get("host", "localhost:8100")
    host_without_port = host.split(":")[0]
    if host_without_port in ("localhost", "127.0.0.1"):
        return canonical
    proto = request.headers.get("x-forwarded-proto", "http")
    return f"{proto}://{host_without_port}"


def paperless_public_base() -> str:
    return os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")


def paperless_server_base() -> str:
    """Paperless vom correspondent-manager-Host (nicht Browser-IP)."""
    api = os.environ.get("PAPERLESS_API_URL", "").strip().rstrip("/")
    if api.endswith("/api"):
        return api[:-4]
    internal = os.environ.get("PAPERLESS_INTERNAL_URL", "").strip().rstrip("/")
    if internal:
        return internal
    return "http://localhost:8000"


def paperless_lan_base(request: Request) -> str | None:
    """Paperless per LAN-IP (:8000) — Request-IP oder Server-LAN-IP."""
    if ip_base := paperless_browser_ip_base(request):
        return ip_base
    if lan := _server_lan_ip():
        return f"http://{lan}:8000"
    return None


def paperless_browser_ip_base(request: Request) -> str | None:
    """Paperless per LAN-IP — nur wenn der Client per IP auf paper.manager zugreift."""
    ip = _request_host_ip(request)
    return f"http://{ip}:8000" if ip else None


def request_api_prefix(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-prefix", "").strip("/")
    if fwd == "corr-manager" or request.url.path.startswith("/corr-manager"):
        return "/corr-manager/api"
    return "/api"


def request_manager_origin(request: Request) -> str:
    host = request.headers.get("host", "localhost:8100")
    proto = request.headers.get("x-forwarded-proto", "http")
    fwd = request.headers.get("x-forwarded-prefix", "").strip("/")
    origin = f"{proto}://{host}"
    if fwd:
        origin = f"{origin}/{fwd}"
    return origin.rstrip("/")


def manager_urls_config(request: Request) -> dict[str, str]:
    """Domain aus PAPERLESS_URL; IP aus Request oder Server-LAN-IP."""
    pub = paperless_public_base()
    out: dict[str, str] = {
        "public": f"{pub}/corr-manager/",
        "home_public": f"{pub}/corr-manager/#home",
    }
    ip = _request_host_ip(request) or _server_lan_ip()
    if ip:
        internal = f"http://{ip}:8100"
        out["internal"] = internal
        out["home_internal"] = f"{internal}/#home"
    return out


def document_urls(doc_id: int, request: Request) -> dict[str, Any]:
    pl_current = effective_paperless_url(request).rstrip("/")
    pl_pub = paperless_public_base()
    pl_ip = paperless_lan_base(request)
    api_prefix = request_api_prefix(request)
    mgr_origin = request_manager_origin(request)
    proxy_path = f"{api_prefix}/proxy/document/{doc_id}/preview/"
    thumb_path = f"{api_prefix}/proxy/document/{doc_id}/thumb/"

    def _triplet(path_suffix: str) -> dict[str, str]:
        return {
            "current": f"{pl_current}{path_suffix}",
            "public": f"{pl_pub}{path_suffix}",
            **({"ip": f"{pl_ip}{path_suffix}"} if pl_ip else {}),
        }

    return {
        "doc_id": doc_id,
        "details": _triplet(f"/documents/{doc_id}/details"),
        "pdf_external": _triplet(f"/api/documents/{doc_id}/preview/"),
        "pdf_embed": f"{mgr_origin}{proxy_path}",
        "pdf_embed_path": proxy_path,
        "thumb_embed": f"{mgr_origin}{thumb_path}",
        "thumb_embed_path": thumb_path,
    }


def build_config_links(request: Request, handbuch_doc_id: int | None = None) -> dict[str, Any]:
    links: dict[str, Any] = {
        "api_prefix": request_api_prefix(request),
        "manager_origin": request_manager_origin(request),
        "paperless": {
            "current": effective_paperless_url(request),
            "public": paperless_public_base(),
            "server": paperless_server_base(),
        },
        "manager": manager_urls_config(request),
    }
    if handbuch_doc_id is not None:
        links["handbuch"] = document_urls(handbuch_doc_id, request)
    return links
