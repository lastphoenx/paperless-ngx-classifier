"""
Nachträgliche volle post_consume-Pipeline für bestehende Paperless-Dokumente.

Läuft post_consume.py als **eigenen Subprozess** (main() nutzt sys.exit — darf nicht
im uvicorn-Worker laufen). Gleiches Muster wie manueller CT121-Lauf, aber aus paper.manager.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("post_consume_runner")

_jobs_lock = threading.Lock()
_jobs: dict[int, dict] = {}


def _job_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pipeline_job_set(document_id: int, **fields) -> None:
    with _jobs_lock:
        prev = _jobs.get(document_id, {})
        _jobs[document_id] = {
            **prev,
            **fields,
            "document_id": document_id,
            "updated_at": _job_now(),
        }


def pipeline_job_get(document_id: int) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(document_id)
        return dict(job) if job else None


def _scripts_dir() -> Path:
    return Path(os.environ.get("PAPERLESS_SCRIPTS_DIR", "/opt/paperless-scripts"))


def _post_consume_script() -> Path:
    return Path(os.environ.get("POST_CONSUME_SCRIPT", str(_scripts_dir() / "post_consume.py")))


def _pipeline_python() -> str:
    venv_py = _scripts_dir() / "venv" / "bin" / "python3"
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable


def _paperless_api_base() -> str:
    return (
        os.environ.get("PAPERLESS_INTERNAL_URL")
        or os.environ.get("PAPERLESS_URL")
        or "http://localhost:8000"
    ).rstrip("/")


def preflight_pipeline_document(document_id: int) -> dict:
    """Dokument + Token + Script vor async-Start prüfen."""
    token = os.environ.get("PAPERLESS_TOKEN") or os.environ.get("PAPERLESS_API_TOKEN")
    if not token:
        return {"ok": False, "error": "PAPERLESS_TOKEN nicht konfiguriert (.env)"}

    script = _post_consume_script()
    if not script.is_file():
        return {"ok": False, "error": f"post_consume.py nicht gefunden: {script}"}

    try:
        import requests

        r = requests.get(
            f"{_paperless_api_base()}/api/documents/{document_id}/",
            headers={"Authorization": f"Token {token}"},
            timeout=30,
        )
        if r.status_code == 404:
            return {"ok": False, "error": f"Dokument #{document_id} nicht in Paperless"}
        r.raise_for_status()
        doc = r.json()
    except Exception as e:
        log.exception("Pipeline preflight #%s", document_id)
        return {"ok": False, "error": f"Dokument #{document_id} nicht lesbar: {e}"}

    title = (doc.get("title") or f"Dokument_{document_id}").strip()
    return {
        "ok": True,
        "document_id": document_id,
        "title": title,
        "script": str(script),
    }


def reprocess_pipeline_document(document_id: int) -> dict:
    """Volle Klassifizierungs-Pipeline für ein bestehendes Dokument."""
    pre = preflight_pipeline_document(document_id)
    if not pre.get("ok"):
        return {"ok": False, "error": pre.get("error", "Preflight fehlgeschlagen")}

    script = _post_consume_script()
    env = os.environ.copy()
    env["DOCUMENT_ID"] = str(document_id)
    env["DOCUMENT_FILE_NAME"] = pre.get("title") or f"Dokument_{document_id}.pdf"
    env.pop("DOCUMENT_SOURCE_PATH", None)

    timeout = int(os.environ.get("PIPELINE_REPROCESS_TIMEOUT", "900"))
    log.info("Pipeline-Reprocess start: Dok #%s (%s)", document_id, script)

    try:
        proc = subprocess.run(
            [_pipeline_python(), str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"Timeout nach {timeout}s — Log: /opt/paperless-scripts/logs/post_consume_v12.log",
        }
    except Exception as e:
        log.exception("Pipeline subprocess #%s", document_id)
        return {"ok": False, "error": str(e)}

    tail = "\n".join(filter(None, [proc.stderr, proc.stdout]))[-2500:]
    if proc.returncode != 0:
        hint = tail.strip() or f"Exit-Code {proc.returncode}"
        return {"ok": False, "error": hint}

    try:
        import post_consume as pc  # noqa: WPS433

        version = pc.POST_CONSUME_VERSION
    except Exception:
        version = "?"

    msg = f"Pipeline abgeschlossen (pipe v{version}) — Dok #{document_id}"
    if tail:
        log.info("Pipeline-Reprocess #%s output tail:\n%s", document_id, tail[-800:])
    return {"ok": True, "message": msg, "pipe_version": version}


def pipeline_job_run(document_id: int) -> None:
    pipeline_job_set(
        document_id,
        status="running",
        message="Pipeline läuft (Vision + LLM, mehrere Minuten)…",
    )
    try:
        result = reprocess_pipeline_document(document_id)
        if result.get("ok"):
            pipeline_job_set(
                document_id,
                status="done",
                message=result.get("message", "Pipeline abgeschlossen"),
                pipe_version=result.get("pipe_version"),
            )
        else:
            err = result.get("error", "Pipeline fehlgeschlagen")
            pipeline_job_set(document_id, status="error", error=err, message=err)
    except Exception as e:
        log.exception("Pipeline Job #%s", document_id)
        pipeline_job_set(document_id, status="error", error=str(e), message=str(e))
