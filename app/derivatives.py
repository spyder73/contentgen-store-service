"""Image derivative helpers — small thumbnails for the media library grid.

The library grid renders one cell per asset; loading the full-resolution
original (often 1024px+) into every cell is the dominant payload cost. This
module produces a small webp thumbnail (≤512px long edge) from an image's
original bytes so the grid can serve the derivative instead.

Design notes:

* **Images only.** Video/audio bytes have no cheap in-process thumbnail; for
  those ``make_thumbnail`` returns ``None`` and callers fall back to the
  original (the grid already lazy-loads + posters video).
* **Graceful degradation.** Pillow is the only dependency. If it is missing, or
  the bytes are not a decodable image, every function returns ``None`` rather
  than raising — a missing thumbnail simply falls back to the original, so the
  feature can never break list/serve paths.
* **Defensive decoding.** ``Image.MAX_IMAGE_PIXELS`` guards against decompression
  bombs; truncated/corrupt images are caught and treated as "no thumbnail".
"""
from __future__ import annotations

import base64
import io
import logging

logger = logging.getLogger(__name__)

# Long-edge cap for the grid thumbnail. 512px covers a 2x DPR cell at typical
# grid densities while keeping each derivative to tens of KB.
THUMBNAIL_MAX_EDGE = 512
THUMBNAIL_CONTENT_TYPE = "image/webp"
# WEBP quality: 80 is visually lossless at thumbnail scale with a small payload.
_WEBP_QUALITY = 80

# Long-edge cap for the *micro* thumbnail — a tiny webp used purely as a blur-up
# placeholder painted under the real cell. ~28px keeps the base64 data-URI to a
# few hundred bytes, small enough to inline in the list JSON for every row.
MICRO_THUMBNAIL_MAX_EDGE = 28
# Lower quality is fine — the micro-thumb is rendered heavily CSS-blurred.
_MICRO_WEBP_QUALITY = 45

try:  # Pillow is declared in requirements.txt; degrade gracefully if absent.
    from PIL import Image, ImageOps

    # Cap decoded pixels to defuse decompression-bomb uploads (well above any
    # legitimate generated/uploaded asset).
    Image.MAX_IMAGE_PIXELS = 64_000_000
    _PIL_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when Pillow is missing
    _PIL_AVAILABLE = False


def is_image_content_type(content_type: str | None) -> bool:
    """True when ``content_type`` denotes a raster image we can thumbnail.

    SVG is excluded: it is XML, not a raster Pillow can resize, and is served
    as the original anyway.
    """
    if not content_type:
        return False
    ct = content_type.split(";", 1)[0].strip().lower()
    if not ct.startswith("image/"):
        return False
    return "svg" not in ct


def make_thumbnail(
    data: bytes,
    content_type: str | None,
    max_edge: int = THUMBNAIL_MAX_EDGE,
) -> tuple[bytes, str] | None:
    """Encode a ≤``max_edge`` webp thumbnail from image ``data``.

    Returns ``(thumbnail_bytes, content_type)`` or ``None`` when a thumbnail
    cannot (or need not) be produced — Pillow missing, non-image content type,
    undecodable bytes, or an image already small enough that a derivative would
    not save payload.
    """
    if not _PIL_AVAILABLE or not data:
        return None
    if not is_image_content_type(content_type):
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            # Respect EXIF orientation so portrait phone uploads aren't sideways.
            img = ImageOps.exif_transpose(img)
            width, height = img.size
            if width <= 0 or height <= 0:
                return None
            # Already small enough — a derivative would only add storage with no
            # payload win; the original is fine for the grid.
            if max(width, height) <= max_edge:
                return None
            # Flatten alpha/palette onto white so webp encodes a clean RGB frame
            # (webp supports alpha, but generated grids read better composited).
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                background = Image.new("RGBA", img.size, (255, 255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background.convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((max_edge, max_edge), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="WEBP", quality=_WEBP_QUALITY, method=4)
            return buffer.getvalue(), THUMBNAIL_CONTENT_TYPE
    except Exception as exc:  # corrupt/truncated/unsupported image — no thumbnail
        logger.debug("thumbnail generation failed: %s", exc)
        return None


def make_micro_thumbnail(
    data: bytes,
    content_type: str | None,
    max_edge: int = MICRO_THUMBNAIL_MAX_EDGE,
) -> str | None:
    """Encode a tiny webp blur-up placeholder as a base64 ``data:`` URI.

    Unlike :func:`make_thumbnail`, this *always* produces output for a decodable
    image — even one already smaller than ``max_edge`` — because the micro-thumb
    is a placeholder painted (CSS-blurred) under the real cell, not a payload
    optimisation. Returns ``None`` only when Pillow is missing, the content type
    is non-image, or the bytes are undecodable (caller falls back to a neutral
    cell). The returned string is a ready-to-use ``data:image/webp;base64,…`` URI
    suitable for inlining directly in the media list JSON.
    """
    if not _PIL_AVAILABLE or not data:
        return None
    if not is_image_content_type(content_type):
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            width, height = img.size
            if width <= 0 or height <= 0:
                return None
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                background = Image.new("RGBA", img.size, (255, 255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background.convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")
            # thumbnail() only shrinks; for an already-tiny image it leaves the
            # size as-is, which is fine — the data-URI stays small either way.
            img.thumbnail((max_edge, max_edge), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="WEBP", quality=_MICRO_WEBP_QUALITY, method=4)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:{THUMBNAIL_CONTENT_TYPE};base64,{encoded}"
    except Exception as exc:  # corrupt/truncated/unsupported image — no micro-thumb
        logger.debug("micro-thumbnail generation failed: %s", exc)
        return None
