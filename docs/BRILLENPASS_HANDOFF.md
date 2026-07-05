# Brillenpass — Handoff (Chat 2026-07-05)

Kurzüberblick für Fortsetzung in neuem Chat. Repo: `paperless-ngx-classifier`, Deploy CT121 `/opt/paperless-scripts/`, UI `http://192.168.131.31:8100`.

**Stand Git `main`:** bis Commit mit Fix `_BG_EXECUTOR` + UI-Cache (UI `2.76`, be `2.38`, 2026-07-05).

---

## Testfall

| Feld | Wert |
|---|---|
| Dokument | Paperless **#3563** (McOptic Brillenpass, Monika) |
| Parser-Override | `mcoptic_brillenpass` |
| Erwartung | Review-Eintrag oben im Tab Brillenpass nach ~1–2 Min Vision |

---

## Symptome (unresolved)

1. **Review-Eintrag erscheint nicht** trotz grüner/positiver UI-Meldung (früher).
2. **`JSON.parse: unexpected character`** beim Trigger (Proxy-Timeout bei sync Vision) — behoben durch async Trigger.
3. **`'bool' object has no attribute 'get'`** nach s1+s2 — Crash in `diagnose_brillenpass_extraction` (behoben `57de419`).
4. **Web-UI hängt / GET `/` = 0 Bytes** — **Ursache:** `asyncio.to_thread(_session_valid)` im Default-Pool vor jedem Request. **Fix:** `GET /` ohne Auth-Middleware, Session in `_AUTH_EXECUTOR` mit `requests`-Timeout.

---

## Audit-Log (Auszug Dok #3563)

Pfad: `/opt/paperless-scripts/training/audit_log.jsonl`

```bash
grep '"document_id": 3563' .../audit_log.jsonl | grep brillenpass
```

Beobachtungen:

- **Stufe 1 (Parser):** `snapshot: {}` — `mcoptic_brillenpass` liefert auf OCR **keine Werte** (Hauptproblem Qualität).
- **Stufe 2 (Vision):** `has_image: true`, Werte plausibel (Sph/Cyl/Achse, `gueltig_ab`, Auftrag).
- **Vision-Fehler:** PD in `prisma`/`basis` (`9.5`+`2` statt `29.5`), fake ADD (`3`, `basis: ADD`), `pd.links: null`.
- Nach bool-Crash: **s1+s2 ohne `brillenpass_merged`** → Pipeline brach vor Review ab.
- Neueste Läufe (21:44+): s2 ok, merged fehlt teils noch — Log auf CT121 prüfen nach Deploy `57de419`+`3342511`.

---

## Commits dieser Session (chronologisch)

| Commit | Inhalt |
|---|---|
| `af79d6c` | Brillenpass-Trigger async (BackgroundTasks), UI `_fetchJson` |
| `c854093` | Job-Status + Polling, Meldung wenn Dok schon in Review |
| `57de419` | **Fix:** `diagnose_brillenpass_extraction` — `.get()` auf bool |
| `3342511` | Vision in `asyncio.to_thread`, `/health` ohne Auth, Session-Check non-blocking |
| (neu) | `_BG_EXECUTOR` statt Default-Pool, UI-HTML-Cache, Session-Cache 30s — UI `2.76` / be `2.38` |

Frühere Session (bereits auf main): PDF per API, Parser-Merge, Gültig-ab, Diagnose-Konflikte, IndentationError-Hotfix `75699ba`.

---

## Architektur Trigger (aktuell)

```
POST /api/brillenpass/trigger/{id}
  → preflight_brillenpass_document()     # sync: Dok, OCR, Person, ggf. «bereits in Review»
  → background_tasks → _run_brillenpass_bg()  # Thread
       → reprocess_brillenpass_document()
            → parse (Stufe 1) → vision (Stufe 2, ~120s) → merge → diagnose → write_pending
GET /api/brillenpass/trigger-status/{id}   # UI pollt alle 3s
```

**Wichtig:** `PAPERLESS_MEDIA_ROOT` / API-Download für PDF-Bild. Ohne Bild: Vision blockiert (leeres `{}`).

---

## Offene Punkte (Priorität)

1. **UI/Service auf CT121 verifizieren** nach Deploy `_BG_EXECUTOR`-Fix:
   - `systemctl status correspondent-manager`
   - `journalctl -u correspondent-manager -n 100 --no-pager`
   - `curl -s http://127.0.0.1:8100/health`
   - `wc -c /opt/paperless-scripts/paper_manager_ui.html` (erwarten ~250 KB)
   - Deploy-Stand: `grep -m1 UI_VERSION /opt/paperless-scripts/correspondent_manager_app.py`
2. **McOptic Parser Stufe 1** — OCR liefert nichts; Parser-Regex/OCR-Snippet prüfen (`parse_mcoptic_pass`).
3. **Vision-Prompt** — `prisma`/`basis` bei leerer Karte weglassen; PD nur in `pd.*`; McOptic fern-only explizit.
4. **PD-Split-Sanitizer** — `9.5`+`2` → `29.5` erkennen.
5. **Tests auf CT121:** `python3 -m pytest tests/test_brillenpass_parsers.py`

---

## Deploy

```bash
cd /opt/paperless-ngx-classifier && git pull
./scripts/deploy-to-ct121.sh --no-docker
systemctl restart correspondent-manager
```

Env prüfen: `PAPERLESS_MEDIA_ROOT=/mnt/paperless-media`, `OLLAMA_MODEL_VISION`, `VISION_TIMEOUT=120`.

---

## Schlüsseldateien

| Datei | Rolle |
|---|---|
| `brillenpass_parser.py` | Parser, merge, `diagnose_brillenpass_extraction` |
| `brillenpass_runner.py` | Trigger, preflight, Job-Status |
| `post_consume.py` | Vision-Prompt, `vision_brillenpass_analyze`, Pipeline consume |
| `correspondent_manager_app.py` | API, async Trigger, Thread-Wrapper |
| `paper_manager_ui.html` | Tab Brillenpass, `_fetchJson`, Polling |
