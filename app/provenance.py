"""XMP AI-provenance injection for stored image originals.

EU AI Act Art. 50 asks for a machine-readable marking on AI-generated content.
For images that marking is an XMP packet carrying the IPTC
``DigitalSourceType = trainedAlgorithmicMedia`` value — the same field Google,
OpenAI and Adobe stamp, and the one platforms read to auto-label AI content.

The injection is pure byte surgery on the container (JPEG APP1 segment, PNG
iTXt chunk, WebP XMP chunk): pixels are never re-encoded, so the stored
original stays visually byte-identical. Every path degrades gracefully — any
parse failure, unsupported format, or already-present XMP returns the input
unchanged, mirroring the contract in ``derivatives.py``: this module must never
corrupt bytes or raise into the persist path. Re-uploads of already-stamped
bytes (e.g. ownership-transfer re-uploads) are therefore idempotent.
"""

from __future__ import annotations

import struct
import zlib
from xml.sax.saxutils import escape

_IPTC_AI_SOURCE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
_XMP_HEADER_JPEG = b"http://ns.adobe.com/xap/1.0/\x00"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_XMP_KEYWORD = b"XML:com.adobe.xmp"
# Presence probe shared by all formats: our own packets and third-party AI
# markings both carry the IPTC source URI, so finding it means "already marked".
_ALREADY_MARKED = _IPTC_AI_SOURCE.encode("ascii")


def build_xmp_packet(provider: str | None, model: str | None) -> bytes:
    """The XMP packet: IPTC digital source type + a plain-language notice."""
    origin = "/".join(part for part in ((provider or "").strip(), (model or "").strip()) if part)
    notice = "AI-generated content (EU AI Act Art. 50 transparency notice)."
    if origin:
        notice += f" Model: {origin}."
    xml = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '    xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
        '    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"\n'
        '    xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/"\n'
        '    xmp:CreatorTool="ContentGen"\n'
        '    photoshop:Credit="ContentGen"\n'
        f'    Iptc4xmpExt:DigitalSourceType="{_IPTC_AI_SOURCE}">\n'
        "   <dc:description><rdf:Alt>"
        f'<rdf:li xml:lang="x-default">{escape(notice)}</rdf:li>'
        "</rdf:Alt></dc:description>\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        # Padding the spec recommends so editors can update in place.
        + (" " * 512) + '\n<?xpacket end="w"?>'
    )
    return xml.encode("utf-8")


def stamp_ai_provenance(data: bytes, metadata: dict | None) -> bytes:
    """Return ``data`` with the AI-provenance XMP packet embedded, when due.

    Only items whose metadata carries ``ai_generated: true`` (stamped by the Go
    backend before the byte upload) are touched — user uploads never carry the
    key. Dispatch is by magic bytes, not the request mime, because generated
    uploads may arrive as ``application/octet-stream``.
    """
    try:
        meta = metadata or {}
        if meta.get("ai_generated") is not True or not data:
            return data
        # Format by magic bytes first: video/audio bytes (also ai_generated)
        # exit here without the marker scan below touching them.
        if data.startswith(b"\xff\xd8"):
            inject = _inject_jpeg
        elif data.startswith(_PNG_SIGNATURE):
            inject = _inject_png
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            inject = _inject_webp
        else:
            return data
        if _ALREADY_MARKED in data:
            return data
        return inject(data, build_xmp_packet(meta.get("ai_provider"), meta.get("ai_model")))
    except Exception:
        return data


def _inject_jpeg(data: bytes, packet: bytes) -> bytes:
    """Insert an APP1/XMP segment after the leading APPn run (post-SOI)."""
    payload = _XMP_HEADER_JPEG + packet
    if len(payload) + 2 > 0xFFFF:
        return data
    offset = 2
    # Skip the existing APP0..APP15 segments so EXIF (APP1) stays first, as the
    # XMP spec asks; stop at the first structural marker. Anything truncated —
    # an APP header or length overrunning the buffer, or no marker where one
    # belongs — leaves the bytes untouched rather than stamping garbage.
    while offset + 2 <= len(data) and data[offset] == 0xFF and 0xE0 <= data[offset + 1] <= 0xEF:
        if offset + 4 > len(data):
            return data
        end = offset + 2 + struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        if end > len(data):
            return data
        offset = end
    if offset + 2 > len(data) or data[offset] != 0xFF:
        return data
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return data[:offset] + segment + data[offset:]


def _inject_png(data: bytes, packet: bytes) -> bytes:
    """Insert an iTXt XMP chunk right after IHDR."""
    if data[12:16] != b"IHDR":
        return data
    ihdr_end = 8 + 4 + 4 + 13 + 4  # signature + length + type + IHDR data + crc
    # iTXt: keyword NUL, compression flag 0, method 0, empty language NUL,
    # empty translated keyword NUL, then the UTF-8 text.
    payload = _PNG_XMP_KEYWORD + b"\x00\x00\x00\x00\x00" + packet
    chunk = (
        struct.pack(">I", len(payload))
        + b"iTXt"
        + payload
        + struct.pack(">I", zlib.crc32(b"iTXt" + payload) & 0xFFFFFFFF)
    )
    return data[:ihdr_end] + chunk + data[ihdr_end:]


def _inject_webp(data: bytes, packet: bytes) -> bytes:
    """Append an XMP chunk and flag it in VP8X. No VP8X → leave untouched.

    Building a VP8X header from a bare VP8/VP8L stream needs canvas metadata
    reconstruction; not worth the risk for the rare simple-WebP original.
    """
    if data[12:16] != b"VP8X":
        return data
    flags_offset = 20  # RIFF(12) + fourcc(4) + size(4) → first VP8X payload byte
    body = packet + (b"\x00" if len(packet) % 2 else b"")
    chunk = b"XMP " + struct.pack("<I", len(packet)) + body
    out = bytearray(data)
    out[flags_offset] |= 0x04  # VP8X XMP flag
    out += chunk
    riff_size = len(out) - 8
    out[4:8] = struct.pack("<I", riff_size)
    return bytes(out)
