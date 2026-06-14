"""Unit tests for the image-thumbnail derivative helper (`app.derivatives`).

These exercise the pure-Pillow encode path on synthetic in-memory images. When
Pillow is not installed the module degrades to returning None for everything;
the PIL-dependent cases are skipped in that environment while the
graceful-degradation contract is still asserted.
"""
from __future__ import annotations

import io

import pytest

from app import derivatives
from app.derivatives import (
    THUMBNAIL_CONTENT_TYPE,
    is_image_content_type,
    make_thumbnail,
)

pil = pytest.importorskip("PIL", reason="Pillow not installed in this env") if derivatives._PIL_AVAILABLE else None


def _png_bytes(width: int, height: int, color=(200, 30, 30)) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_rgba_bytes(width: int, height: int) -> bytes:
    from PIL import Image

    img = Image.new("RGBA", (width, height), (10, 200, 10, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── content-type gate ─────────────────────────────────────────────────────────

class TestIsImageContentType:
    def test_image_types_accepted(self):
        assert is_image_content_type("image/png")
        assert is_image_content_type("image/jpeg")
        assert is_image_content_type("image/webp")
        assert is_image_content_type("image/png; charset=binary")

    def test_non_image_rejected(self):
        assert not is_image_content_type("video/mp4")
        assert not is_image_content_type("audio/mpeg")
        assert not is_image_content_type("application/octet-stream")
        assert not is_image_content_type(None)
        assert not is_image_content_type("")

    def test_svg_rejected(self):
        # SVG is XML, not a raster Pillow can resize.
        assert not is_image_content_type("image/svg+xml")


# ── graceful degradation (always holds) ───────────────────────────────────────

class TestMakeThumbnailDegradation:
    def test_empty_bytes_returns_none(self):
        assert make_thumbnail(b"", "image/png") is None

    def test_non_image_returns_none(self):
        assert make_thumbnail(b"not-an-image", "video/mp4") is None

    def test_garbage_image_bytes_returns_none(self):
        # Claims to be a png but isn't decodable → no thumbnail, no exception.
        assert make_thumbnail(b"\x89PNG\r\n\x1a\nGARBAGE", "image/png") is None


# ── PIL-dependent encode path ─────────────────────────────────────────────────

@pytest.mark.skipif(not derivatives._PIL_AVAILABLE, reason="Pillow not installed")
class TestMakeThumbnailEncode:
    def test_large_image_is_downscaled_to_webp(self):
        original = _png_bytes(2000, 1000)
        result = make_thumbnail(original, "image/png", max_edge=512)
        assert result is not None
        data, content_type = result
        assert content_type == THUMBNAIL_CONTENT_TYPE
        # The derivative must be smaller than the original payload.
        assert len(data) < len(original)
        # And its long edge must be capped at max_edge.
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            assert max(img.size) <= 512
            assert img.format == "WEBP"
            # Aspect ratio preserved (2:1).
            assert img.size == (512, 256)

    def test_small_image_returns_none(self):
        # Already ≤ max_edge: a derivative would only add storage, so skip it.
        original = _png_bytes(200, 200)
        assert make_thumbnail(original, "image/png", max_edge=512) is None

    def test_rgba_is_flattened_and_encoded(self):
        original = _png_rgba_bytes(1024, 768)
        result = make_thumbnail(original, "image/png", max_edge=256)
        assert result is not None
        data, content_type = result
        assert content_type == THUMBNAIL_CONTENT_TYPE
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            assert max(img.size) <= 256

    def test_jpeg_input_supported(self):
        from PIL import Image

        img = Image.new("RGB", (1500, 1500), (12, 34, 56))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        result = make_thumbnail(buf.getvalue(), "image/jpeg", max_edge=512)
        assert result is not None
        _, content_type = result
        assert content_type == THUMBNAIL_CONTENT_TYPE
