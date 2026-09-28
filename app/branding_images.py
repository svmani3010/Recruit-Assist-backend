"""Optimize HR branding uploads for fast web delivery without harsh quality loss."""

from __future__ import annotations

import io
import logging
from typing import Literal, Tuple

from fastapi import HTTPException
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

BrandingKind = Literal["logo", "banner"]

# Tuned for hero banners: sharp enough on retina, much smaller than raw uploads.
_LOGO_MAX_PX = 512
_BANNER_MAX_WIDTH = 1920
_BANNER_MAX_HEIGHT = 720
_LOGO_WEBP_QUALITY = 88
_BANNER_WEBP_QUALITY = 85


def branding_kind_from_metadata(kind: str | None) -> BrandingKind | None:
    normalized = str(kind or "").strip().lower()
    if normalized == "company_logo":
        return "logo"
    if normalized == "company_banner":
        return "banner"
    return None


def optimize_branding_image(raw: bytes, kind: BrandingKind) -> Tuple[bytes, str, str]:
    """Resize and compress a logo or banner to WebP for faster loading."""
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    original_size = len(raw)

    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            img = _to_rgb(img)

            if kind == "logo":
                img.thumbnail(
                    (_LOGO_MAX_PX, _LOGO_MAX_PX),
                    Image.Resampling.LANCZOS,
                )
                quality = _LOGO_WEBP_QUALITY
            else:
                img.thumbnail(
                    (_BANNER_MAX_WIDTH, _BANNER_MAX_HEIGHT),
                    Image.Resampling.LANCZOS,
                )
                quality = _BANNER_WEBP_QUALITY

            buffer = io.BytesIO()
            img.save(
                buffer,
                format="WEBP",
                quality=quality,
                method=4,
                optimize=True,
            )
            optimized = buffer.getvalue()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Branding image optimization failed")
        raise HTTPException(
            status_code=400,
            detail="Could not process image. Upload a valid JPG, PNG, or WEBP file.",
        ) from exc

    if not optimized:
        raise HTTPException(status_code=400, detail="Image optimization produced empty output")

    saved_pct = 0.0
    if original_size > 0:
        saved_pct = max(0.0, (1 - (len(optimized) / original_size)) * 100)

    logger.info(
        "Branding %s optimized: %s KB -> %s KB (%.1f%% smaller)",
        kind,
        original_size // 1024,
        len(optimized) // 1024,
        saved_pct,
    )

    return optimized, "image/webp", ".webp"


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in {"RGB", "L"}:
        return img.convert("RGB") if img.mode == "L" else img

    if img.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", img.size, (255, 255, 255))
        alpha = img.split()[-1]
        background.paste(img.convert("RGBA"), mask=alpha)
        return background

    if img.mode == "P":
        return _to_rgb(img.convert("RGBA"))

    return img.convert("RGB")
