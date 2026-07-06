"""
Brillenpass Stufe 1 — Tesseract TSV + Anker/Spalten-Geometrie (deterministisch).
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import subprocess
import tempfile
from copy import deepcopy
from typing import Any

from brillenpass_parser import (
    _empty_eye_block,
    _norm_val,
    has_brillenpass_values,
    norm_pd_mm,
    plausible_brillenpass_data,
    plausible_refraktion_eye,
    strict_diopter_token,
)

log = logging.getLogger("brillenpass_tsv")

_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "sph": ("sph", "sph.", "sφ", "sp"),
    "cyl": ("cyl", "zyl", "cyl.", "zyl.", "cy1", "zy1"),
    "achse": ("achse", "axe", "achs", "axis", "ach", "a°"),
    "add": ("add", "zusa", "zusatz", "addition"),
    "pd": ("pd", "pupillendistanz"),
}
_HEADER_PREFIX: dict[str, re.Pattern[str]] = {
    "sph": re.compile(r"^sph", re.I),
    "cyl": re.compile(r"^(cyl|zyl|cy1|zy1)", re.I),
    "achse": re.compile(r"^(achse|axe|achs|axis|ach)", re.I),
    "add": re.compile(r"^(add|zusa)", re.I),
    "pd": re.compile(r"^pd", re.I),
}

_RL_RE = re.compile(r"^(r|l|rechts|links)$", re.IGNORECASE)
_NUM_RE = re.compile(r"^[+-]?\d")


def _normalize_header_token(text: str) -> str:
    return re.sub(r"[^a-z0-9°]", "", text.lower())


def header_field_for_token(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    n = _normalize_header_token(raw)
    if not n:
        return None
    for field, syns in _HEADER_SYNONYMS.items():
        if n in syns:
            return field
    for field, pat in _HEADER_PREFIX.items():
        if pat.match(raw.strip()):
            return field
    if n in ("a°",) or raw.strip().upper() in ("A°", "A"):
        return "achse"
    return None


def merge_rl_continuation_lines(zeilen: list[list[dict]]) -> list[list[dict]]:
    """R/L oft allein in Zeile 1, Zahlen in Zeile 2 — zusammenführen."""
    merged: list[list[dict]] = []
    i = 0
    while i < len(zeilen):
        zeile = zeilen[i]
        if i + 1 < len(zeilen):
            side = _row_side(zeile)
            nums_here = _numeric_tokens(zeile)
            nums_next = _numeric_tokens(zeilen[i + 1])
            if side and len(nums_here) < 3 and nums_next and not _row_side(zeilen[i + 1]):
                merged.append(sorted(zeile + zeilen[i + 1], key=lambda x: x["left"]))
                i += 2
                continue
            if (
                len(zeile) == 1
                and _RL_RE.match(zeile[0]["text"].strip())
                and nums_next
            ):
                merged.append(sorted(zeile + zeilen[i + 1], key=lambda x: x["left"]))
                i += 2
                continue
        merged.append(zeile)
        i += 1
    return merged


def gruppiere_nach_top(words: list[dict], tol: int = 12) -> list[list[dict]]:
    zeilen: list[list[dict]] = []
    for w in sorted(words, key=lambda x: (x["top"], x["left"])):
        if zeilen and abs(w["top"] - zeilen[-1][0]["top"]) <= tol:
            zeilen[-1].append(w)
        else:
            zeilen.append([w])
    for zeile in zeilen:
        zeile.sort(key=lambda x: x["left"])
    return zeilen


def run_tesseract_tsv(image_path: str, *, lang: str = "deu", min_conf: int = 30) -> list[dict]:
    """Tesseract --psm 6 TSV → Wortliste mit Geometrie."""
    if not image_path or not os.path.isfile(image_path):
        return []
    try:
        out = subprocess.run(
            ["tesseract", image_path, "-", "--psm", "6", "-l", lang, "tsv"],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout
    except FileNotFoundError:
        log.warning("tesseract nicht installiert — Stufe 1 TSV übersprungen")
        return []
    except subprocess.CalledProcessError as e:
        log.warning("tesseract TSV fehlgeschlagen (%s): %s", image_path, (e.stderr or e.stdout or e)[:300])
        return []
    except Exception as e:
        log.warning("tesseract TSV Fehler: %s", e)
        return []

    words: list[dict] = []
    for row in csv.DictReader(io.StringIO(out), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = int(float(row.get("conf") or 0))
        except (TypeError, ValueError):
            conf = 0
        if conf <= min_conf:
            continue
        try:
            words.append({
                "text": text,
                "left": int(row["left"]),
                "top": int(row["top"]),
                "width": int(row.get("width") or 0),
                "height": int(row.get("height") or 0),
                "conf": conf,
            })
        except (KeyError, TypeError, ValueError):
            continue
    return words


def _pdf_first_page_jpg(pdf_path: str, *, dpi: int = 300) -> str | None:
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp_path = tmp.name
        tmp.close()
        subprocess.run(
            [
                "gs", "-dNOPAUSE", "-dBATCH", "-sDEVICE=jpeg",
                "-dFirstPage=1", "-dLastPage=1", f"-r{dpi}",
                f"-sOutputFile={tmp_path}", pdf_path,
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        return tmp_path
    except Exception as e:
        log.debug("PDF→JPEG für Tesseract fehlgeschlagen: %s", e)
        return None


def run_tesseract_tsv_on_document(document_path: str, *, lang: str = "deu") -> list[dict]:
    """PDF: zuerst gerenderte Seite (300 dpi) — direktes PDF-OCR liefert oft schlechte Tabellen."""
    if document_path.lower().endswith(".pdf"):
        jpg = _pdf_first_page_jpg(document_path)
        if jpg:
            try:
                words = run_tesseract_tsv(jpg, lang=lang)
                if words:
                    return words
            finally:
                try:
                    os.unlink(jpg)
                except OSError:
                    pass
    words = run_tesseract_tsv(document_path, lang=lang)
    if words or not document_path.lower().endswith(".pdf"):
        return words
    jpg = _pdf_first_page_jpg(document_path)
    if not jpg:
        return []
    try:
        return run_tesseract_tsv(jpg, lang=lang)
    finally:
        try:
            os.unlink(jpg)
        except OSError:
            pass


def _header_fields_in_line(zeile: list[dict]) -> dict[str, dict]:
    fields: dict[str, dict] = {}
    for w in zeile:
        field = header_field_for_token(w["text"])
        if field and field not in fields:
            fields[field] = w
    return fields


def find_best_header_row(zeilen: list[list[dict]]) -> tuple[dict[str, dict], int, int]:
    best_fields: dict[str, dict] = {}
    best_count = 0
    best_idx = -1
    for idx, zeile in enumerate(zeilen):
        fields = _header_fields_in_line(zeile)
        if len(fields) > best_count:
            best_count = len(fields)
            best_fields = fields
            best_idx = idx
    return best_fields, best_count, best_idx


def header_field_names(fields: dict[str, dict]) -> list[str]:
    return sorted(fields.keys())


def _diopter_from_token(raw: str) -> str | None:
    return strict_diopter_token(raw)


def _both_fern_eyes(data: dict | None) -> bool:
    fern = (data or {}).get("fern") or {}
    return bool((fern.get("rechts") or {}).get("sph") and (fern.get("links") or {}).get("sph"))


def _score_tsv_extraction(data: dict) -> int:
    from brillenpass_parser import _parser_completeness_score  # noqa: WPS433

    score = _parser_completeness_score(data)
    if _both_fern_eyes(data):
        score += 25
    pd = data.get("pd") or {}
    if pd.get("rechts") and pd.get("links"):
        score += 10
    return score


def count_header_anchors(words: list[dict]) -> int:
    if not words:
        return 0
    _, count, _ = find_best_header_row(gruppiere_nach_top(words))
    return count


def _column_specs(anchor_fields: dict[str, dict]) -> dict[str, dict[str, float]]:
    ordered = sorted(anchor_fields.items(), key=lambda x: x[1]["left"])
    specs: dict[str, dict[str, float]] = {}
    for i, (field, word) in enumerate(ordered):
        left = float(word["left"])
        if i == 0:
            left_tol = (float(ordered[1][1]["left"]) - left) / 2 if len(ordered) > 1 else 40.0
        else:
            left_tol = (left - float(ordered[i - 1][1]["left"])) / 2
        if i == len(ordered) - 1:
            right_tol = left_tol
        else:
            right_tol = (float(ordered[i + 1][1]["left"]) - left) / 2
        specs[field] = {"center": left, "tol": max(left_tol, right_tol, 15.0)}
    return specs


def _row_side(zeile: list[dict]) -> str | None:
    for w in zeile[:5]:
        t = w["text"].strip()
        if _RL_RE.match(t):
            return "rechts" if t[0].lower() == "r" else "links"
        if re.match(r"^R(?:[^a-z]|$)", t, re.I):
            return "rechts"
        if re.match(r"^L(?:[^a-z]|$)", t, re.I):
            return "links"
    return None


def _plausible_pd(val: str | None) -> bool:
    if not val:
        return False
    try:
        n = abs(float(str(val).replace(",", ".").lstrip("+")))
    except ValueError:
        return False
    return 20.0 <= n <= 40.0


def _detect_section(zeilen: list[list[dict]], header_idx: int) -> str:
    section = "fern"
    for zeile in zeilen[max(0, header_idx - 3): header_idx + 1]:
        line = " ".join(w["text"] for w in zeile).lower()
        if re.search(r"\bnah\b|nähe|reading|addition", line):
            section = "naehe"
        elif re.search(r"\bfern\b|distance|weit", line):
            section = "fern"
    return section


def _assign_row_values(zeile: list[dict], column_specs: dict[str, dict[str, float]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for w in zeile:
        t = w["text"].strip()
        if header_field_for_token(t) or _RL_RE.match(t):
            continue
        if not _NUM_RE.match(t.replace(",", ".")):
            continue
        best_field: str | None = None
        best_dist = float("inf")
        for field, spec in column_specs.items():
            dist = abs(float(w["left"]) - spec["center"])
            if dist <= spec["tol"] and dist < best_dist:
                best_dist = dist
                best_field = field
        if best_field and best_field not in values:
            values[best_field] = t
    return values


def _numeric_tokens(zeile: list[dict]) -> list[dict]:
    out: list[dict] = []
    for w in zeile:
        t = w["text"].strip()
        if header_field_for_token(t) or _RL_RE.match(t):
            continue
        if _NUM_RE.match(t.replace(",", ".")):
            out.append(w)
    return out


def _parse_rl_rows_positional(zeilen: list[list[dict]]) -> dict | None:
    """Fallback: R/L-Zeile mit Sph/Cyl/Achse/PD ohne volle Header-Zeile."""
    result: dict[str, Any] = {
        "parser": "tsv_positional",
        "fern": _empty_eye_block(),
        "naehe": _empty_eye_block(),
        "pd": {"rechts": None, "links": None},
    }
    found = 0
    for zeile in zeilen:
        side = _row_side(zeile)
        if not side:
            continue
        nums = _numeric_tokens(zeile)
        if len(nums) < 3:
            continue
        eye = _eye_from_values({
            "sph": nums[0]["text"],
            "cyl": nums[1]["text"],
            "achse": nums[2]["text"],
        })
        if not eye.get("sph"):
            continue
        result["fern"][side] = eye
        pd_v = None
        for w in reversed(nums):
            cand = norm_pd_mm(w["text"])
            if cand:
                pd_v = cand
                break
        if pd_v:
            result["pd"][side] = pd_v
        found += 1
    if found == 0:
        return None
    if not has_brillenpass_values(result) or not plausible_brillenpass_data(result):
        return None
    return result


def _collect_tsv_candidates(
    words: list[dict],
    zeilen: list[list[dict]],
    parser_names: list[str] | None,
) -> list[tuple[int, dict, str]]:
    candidates: list[tuple[int, dict, str]] = []
    for method, fn in (
        ("positional", lambda: _parse_rl_rows_positional(zeilen)),
        ("text", lambda: _parse_tsv_text_fallback(words, parser_names)),
        ("anchors", lambda: parse_by_anchors(words)),
    ):
        try:
            parsed = fn()
        except Exception as e:
            log.debug("TSV %s fehlgeschlagen: %s", method, e)
            continue
        if not parsed or not plausible_brillenpass_data(parsed):
            continue
        candidates.append((_score_tsv_extraction(parsed), parsed, method))
    return candidates


def _parse_tsv_text_fallback(words: list[dict], parser_names: list[str] | None = None) -> dict | None:
    """Regex-Parser auf Tesseract-Fließtext (McOptic/Fielmann/…)."""
    from brillenpass_parser import parse_brillenpass_with_parsers  # noqa: WPS433

    flat = tsv_words_to_text(words)
    if not flat.strip():
        return None
    allowed = parser_names or [
        "mcoptic_brillenpass", "mcoptic_rechnung", "fielmann_brillenpass",
        "fielmann_rechnung", "augenarzt_verordnung", "optik_meyer_rechnung",
    ]
    parsed = parse_brillenpass_with_parsers(flat, allowed)
    if not parsed or not has_brillenpass_values(parsed):
        return None
    if not plausible_brillenpass_data(parsed):
        return None
    parsed = deepcopy(parsed)
    parsed["parser"] = "tsv_text"
    return parsed


def _eye_from_values(vals: dict[str, str]) -> dict[str, str | None]:
    eye: dict[str, str | None] = {}
    if vals.get("sph"):
        eye["sph"] = _diopter_from_token(vals["sph"])
    if vals.get("cyl"):
        eye["cyl"] = _diopter_from_token(vals["cyl"])
    if vals.get("achse") is not None:
        achse = re.sub(r"[^\d]", "", vals["achse"])
        eye["achse"] = achse if achse != "" else None
    if vals.get("add"):
        eye["add"] = _diopter_from_token(vals["add"]) or _norm_val(vals["add"])
    if not eye.get("sph") or not plausible_refraktion_eye(eye):
        return {}
    return eye


def tsv_words_to_text(words: list[dict]) -> str:
    """TSV-Wörter → mehrzeiliger Text (R/L-Zeilen bereits zusammengeführt)."""
    return "\n".join(" ".join(w["text"] for w in zeile) for zeile in _prepare_zeilen(words))


def _prepare_zeilen(words: list[dict]) -> list[list[dict]]:
    return merge_rl_continuation_lines(gruppiere_nach_top(words))


def parse_by_anchors(words: list[dict]) -> dict | None:
    """Header-Anker + R/L-Zeilen → Refraktions-Dict (fern/naehe/pd).

    McOptic: pro Auge eigene Header-Zeile (R/L klebt am Header, Werte in Folgezeile).
    """
    if not words:
        return None
    zeilen = _prepare_zeilen(words)
    header_fields, anchor_count, header_idx = find_best_header_row(zeilen)
    if anchor_count < 3 or header_idx < 0:
        return None

    column_specs = _column_specs(header_fields)
    section = _detect_section(zeilen, header_idx)
    result: dict[str, Any] = {
        "parser": "tsv_anchors",
        "fern": _empty_eye_block(),
        "naehe": _empty_eye_block(),
        "pd": {"rechts": None, "links": None},
    }
    pending_side: str | None = None

    for zeile in zeilen[header_idx:]:
        hdr = _header_fields_in_line(zeile)
        if len(hdr) >= 3:
            column_specs = _column_specs(hdr)
            pending_side = _row_side(zeile)
            continue
        side = _row_side(zeile) or pending_side
        if not side:
            continue
        vals = _assign_row_values(zeile, column_specs)
        eye = _eye_from_values(vals)
        if eye.get("sph") or eye.get("cyl"):
            result[section][side] = eye
        pd_v = norm_pd_mm(vals.get("pd"))
        if pd_v:
            result["pd"][side] = pd_v
        pending_side = None

    if not has_brillenpass_values(result) or not plausible_brillenpass_data(result):
        return None
    return result


def _strip_red_annotations(image_path: str) -> str | None:
    """Rotkanal als Graustufen — roter Stift verschwindet, schwarzer Druck bleibt."""
    try:
        from PIL import Image

        img = Image.open(image_path).convert("RGB")
        r, _g, _b = img.split()
        fd, out = tempfile.mkstemp(suffix="_rchan.png")
        os.close(fd)
        r.save(out)
        return out
    except Exception as e:
        log.debug("Rotkanal fehlgeschlagen: %s", e)
        return None


def _ocr_image_variants(image_path: str) -> tuple[list[tuple[str, bool]], list[str]]:
    """OCR-Pfade (normal + optional Rotkanal) und Temp-Dateien zum Aufräumen."""
    temps: list[str] = []
    jobs: list[tuple[str, bool]] = []

    if image_path.lower().endswith(".pdf"):
        jpg = _pdf_first_page_jpg(image_path)
        if jpg:
            temps.append(jpg)
            jobs.append((jpg, False))
            rchan = _strip_red_annotations(jpg)
            if rchan:
                temps.append(rchan)
                jobs.append((rchan, True))
        if not jobs:
            jobs.append((image_path, False))
    else:
        jobs.append((image_path, False))
        rchan = _strip_red_annotations(image_path)
        if rchan:
            temps.append(rchan)
            jobs.append((rchan, True))
    return jobs, temps


def _pick_best_tsv_candidate(
    words: list[dict],
    parser_names: list[str] | None,
) -> tuple[int, dict | None, str | None]:
    if not words:
        return 0, None, None
    zeilen = _prepare_zeilen(words)
    candidates = _collect_tsv_candidates(words, zeilen, parser_names)
    if not candidates:
        return 0, None, None
    score, parsed, method = max(candidates, key=lambda x: x[0])
    return score, parsed, method


def extract_brillenpass_from_image(
    image_path: str,
    parser_names: list[str] | None = None,
) -> tuple[dict, str, dict]:
    """TSV-Pipeline: (daten, confidence, meta). Normal-OCR + optional Rotkanal-Pass."""
    jobs, temps = _ocr_image_variants(image_path)
    best_words: list[dict] = []
    best_parsed: dict | None = None
    best_method: str | None = None
    best_score = -1
    red_used = False

    try:
        for path, red in jobs:
            words = run_tesseract_tsv(path, lang="deu")
            if len(words) > len(best_words):
                best_words = words
            score, parsed, method = _pick_best_tsv_candidate(words, parser_names)
            if parsed and score > best_score:
                best_score = score
                best_parsed = parsed
                best_method = method
                best_words = words
                red_used = red
    finally:
        for t in temps:
            try:
                os.unlink(t)
            except OSError:
                pass

    if not best_words and image_path.lower().endswith(".pdf"):
        best_words = run_tesseract_tsv_on_document(image_path)

    zeilen = _prepare_zeilen(best_words) if best_words else []
    header_fields, anchor_count, _header_idx = find_best_header_row(zeilen) if zeilen else ({}, 0, -1)
    meta: dict[str, Any] = {
        "header_anchors": anchor_count,
        "header_fields": header_field_names(header_fields),
        "word_count": len(best_words),
        "ocr_source": "jpeg300" if (image_path or "").lower().endswith(".pdf") else "direct",
        "red_channel": red_used,
    }

    if best_parsed:
        meta["score"] = best_score
        meta["method"] = best_method
        if best_method in ("anchors", "positional") and _both_fern_eyes(best_parsed):
            meta["confidence"] = "hoch"
        elif _both_fern_eyes(best_parsed):
            meta["confidence"] = "mittel"
        else:
            meta["confidence"] = "niedrig"
        return best_parsed, meta["confidence"], meta
    if anchor_count >= 3:
        meta["confidence"] = "niedrig"
        meta["tsv_preview"] = tsv_words_to_text(best_words)[:400]
        return {}, "niedrig", meta
    meta["confidence"] = "keine_extraktion"
    if best_words:
        meta["tsv_preview"] = tsv_words_to_text(best_words)[:400]
    return {}, "keine_extraktion", meta


def merge_brillenpass_tsv_with_regex(
    tsv_data: dict | None,
    regex_data: dict | None,
    *,
    ocr_text: str = "",
) -> dict:
    """TSV gewinnt bei Refraktion; Regex füllt Glas/Auftrag/Datum; Nutzung routen."""
    from brillenpass_parser import finalize_brillenpass_buckets  # noqa: WPS433

    base = deepcopy(regex_data) if regex_data else {
        "fern": _empty_eye_block(),
        "naehe": _empty_eye_block(),
        "pd": {"rechts": None, "links": None},
        "glas": {"beschreibung": "", "index": None, "durchmesser": None, "beschichtungen": []},
    }
    if not tsv_data:
        return base

    for dist in ("fern", "naehe"):
        for side in ("rechts", "links"):
            t_eye = (tsv_data.get(dist) or {}).get(side) or {}
            if not t_eye:
                continue
            p_eye = (base.get(dist) or {}).get(side) or {}
            merged_eye = {**p_eye, **{k: v for k, v in t_eye.items() if v}}
            if not (merged_eye.get("sph") or merged_eye.get("cyl")):
                continue
            if not plausible_refraktion_eye(merged_eye):
                base.setdefault(dist, _empty_eye_block())[side] = None
            else:
                base.setdefault(dist, _empty_eye_block())[side] = merged_eye

    t_pd = tsv_data.get("pd") or {}
    p_pd = base.setdefault("pd", {"rechts": None, "links": None})
    for side in ("rechts", "links"):
        pd_v = norm_pd_mm(t_pd.get(side)) or t_pd.get(side)
        if pd_v:
            p_pd[side] = pd_v

    for k in ("gueltig_ab", "auftrag", "rechnung"):
        if tsv_data.get(k) and not base.get(k):
            base[k] = tsv_data[k]

    quellen = []
    if has_brillenpass_values(tsv_data):
        quellen.append("tsv")
    if regex_data and has_brillenpass_values(regex_data):
        quellen.append("regex")
    base["extraktion"] = {
        "quelle": "+".join(quellen) if quellen else "merged",
        "confidence": "hoch" if "tsv" in quellen else "mittel",
    }
    if tsv_data.get("parser"):
        base["parser"] = tsv_data["parser"]
    if regex_data and regex_data.get("parser", "").startswith("mcoptic"):
        base["parser"] = regex_data["parser"]
    return finalize_brillenpass_buckets(base, ocr_text=ocr_text)
