"""PDF-Seiten rendern und für HTR croppen (trim, horizontal bands)."""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from handwriting_vision import HtrProfileConfig

log = logging.getLogger(__name__)


def render_page_image(pdf_path: str, page: int, dpi: int):
    """PDF-Seite via Ghostscript → PIL Image."""
    from PIL import Image

    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run(
            [
                "gs", "-dNOPAUSE", "-dBATCH", "-sDEVICE=jpeg",
                f"-dFirstPage={page}", f"-dLastPage={page}", f"-r{dpi}",
                f"-sOutputFile={tmp_path}", pdf_path,
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
        img = Image.open(tmp_path).convert("RGB")
        os.unlink(tmp_path)
        return img
    except Exception as e:
        log.warning("render_page_image fehlgeschlagen (Seite %d): %s", page, e)
        try:
            if "tmp_path" in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        return None


def trim_whitespace(img, *, threshold: int = 245, padding: int = 8):
    """Weiße Ränder entfernen."""
    from PIL import ImageOps

    gray = ImageOps.grayscale(img)
    bbox = gray.point(lambda p: 255 if p > threshold else 0).getbbox()
    if not bbox:
        return img
    left = max(0, bbox[0] - padding)
    top = max(0, bbox[1] - padding)
    right = min(img.width, bbox[2] + padding)
    bottom = min(img.height, bbox[3] + padding)
    return img.crop((left, top, right, bottom))


def normalize_contrast(img):
    """Leichte Kontrast-Normalisierung für Scans."""
    from PIL import ImageFilter, ImageOps

    gray = ImageOps.autocontrast(img.convert("L"))
    sharp = gray.filter(ImageFilter.SHARPEN)
    return sharp.convert("RGB")


def crop_horizontal_bands(
    img,
    fractions: list[float],
    *,
    padding_px: int = 12,
) -> list[tuple[str, object]]:
    """Horizontale Streifen — fractions sind Y-Anteile [0.0, ..., 1.0]."""
    if len(fractions) < 2:
        return [("trim", img)]
    w, h = img.size
    out: list[tuple[str, object]] = []
    for i in range(len(fractions) - 1):
        y0 = max(0, int(h * fractions[i]) - (padding_px if i else 0))
        y1 = min(h, int(h * fractions[i + 1]) + (padding_px if i + 1 < len(fractions) - 1 else 0))
        if y1 <= y0:
            continue
        band = img.crop((0, y0, w, y1))
        out.append((f"band_{i}", band))
    return out or [("trim", img)]


def image_to_b64_jpeg(img, *, quality: int = 92) -> str:
    import io

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def apply_crop_to_image(img, crop_mode: str, profile: "HtrProfileConfig"):
    """Einzelnes Bild nach crop_mode verarbeiten."""
    if crop_mode == "none":
        return [("full", img)]
    trimmed = trim_whitespace(img)
    if profile.enhance:
        trimmed = normalize_contrast(trimmed)
    if crop_mode == "trim":
        return [("trim", trimmed)]
    if crop_mode == "horizontal":
        return crop_horizontal_bands(
            trimmed,
            profile.horizontal_bands,
            padding_px=profile.band_padding_px,
        )
    return [("trim", trimmed)]


def render_page_variants(
    pdf_path: str,
    page: int,
    profile: "HtrProfileConfig",
    crop_mode_effective: str,
) -> list[tuple[str, str]]:
    """PDF-Seite rendern und Varianten als base64-JPEG zurückgeben."""
    dpi = profile.dpi or 220
    img = render_page_image(pdf_path, page, dpi)
    if img is None:
        return []
    variants = apply_crop_to_image(img, crop_mode_effective, profile)
    return [(vid, image_to_b64_jpeg(im)) for vid, im in variants]
