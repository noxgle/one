"""Image attachment domain, validation, and local blob storage.

Images are stored as immutable content-addressed blobs (SHA-256).  Session
JSONL records only lightweight ``AttachmentRef`` entries — never Base64
payloads or full source paths.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import struct
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

_MAX_SOURCE_BYTES = 10 * 1024 * 1024          # 10 MB decoded source
_MAX_BASE64_BYTES = 5 * 1024 * 1024          # 5 MiB encoded Base64
_MAX_IMAGES_PER_PROMPT = 4

# Magic-byte signatures for supported formats.
_MAGIC_SIGNATURES: dict[bytes, str] = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"RIFF": "image/webp",   # chunk-based; check further below
}

_WEBP_RIFF = b"WEBP"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttachmentRef:
    """Immutable reference stored in session JSONL.

    Only ``blob_hash`` and ``mime`` are persisted — no source paths or
    internal storage paths leak into the session file.
    """

    blob_hash: str          # SHA-256 hex digest of the raw bytes
    mime: str               # detected MIME (image/png, image/jpeg, image/webp)
    size: int = 0           # raw byte count of the stored blob
    width: int | None = None
    height: int | None = None


@dataclass
class AttachmentInput:
    """Normalized input ready for import (validated path + MIME)."""

    path: str
    mime: str = ""


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class AttachmentError(ValueError):
    """Base for all attachment validation/storage errors."""


class AttachmentValidationError(AttachmentError):
    """Raised when an image fails validation."""


class AttachmentStorageError(AttachmentError):
    """Raised on blob-store failures."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _detect_mime_magic(raw: bytes) -> str | None:
    """Return MIME from magic bytes, or None on mismatch."""
    for sig, mime in _MAGIC_SIGNATURES.items():
        if raw[: len(sig)] == sig:
            # WebP is RIFF....WEBP — check the FOURCC.
            if mime == "image/webp" and len(raw) >= 12:
                if raw[8:12] != _WEBP_RIFF:
                    return None
            return mime
    return None


def _detect_mime_mimetype(path: str) -> str | None:
    """Fuzzy MIME guess from file extension / path."""
    guess = mimetypes.guess_type(path)[0] or ""
    if guess.startswith("image/"):
        return guess
    return None


def _extract_dimensions(raw: bytes, mime: str) -> tuple[int | None, int | None]:
    """Return (width, height) for supported image formats."""
    try:
        if mime == "image/png" and len(raw) >= 24:
            w = struct.unpack(">I", raw[16:20])[0]
            h = struct.unpack(">I", raw[20:24])[0]
            if w > 0 and h > 0:
                return w, h
        if mime == "image/jpeg" and len(raw) >= 2:
            # Walk SOF markers (0xFFC0..0xFFC3, 0xFFC5..0xFFC7, 0xFFC9..0xFFCB, 0xFFCD)
            i = 2
            while i + 5 < len(raw):
                if raw[i] != 0xFF:
                    break
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = struct.unpack(">H", raw[i + 5 : i + 7])[0]
                    w = struct.unpack(">H", raw[i + 7 : i + 9])[0]
                    if w > 0 and h > 0:
                        return w, h
                    break
                seg_len = struct.unpack(">H", raw[i + 2 : i + 4])[0]
                i += 2 + seg_len
        if mime == "image/webp" and len(raw) >= 30 and raw[8:12] == _WEBP_RIFF:
            chunk = raw[12:16]
            if chunk in (b"VP8 ", b"VP8L", b"VP8X"):
                if chunk == b"VP8 ":
                    # Simple format: skip 10 bytes of VP8 header then read LE dims
                    if len(raw) >= 24:
                        w = struct.unpack("<H", raw[22:24])[0] & 0x3FFF
                        h = struct.unpack("<H", raw[24:26])[0] & 0x3FFF
                        if w > 0 and h > 0:
                            return w, h
                elif chunk == b"VP8L":
                    if len(raw) >= 25:
                        b = raw[21]
                        w = ((struct.unpack("<I", raw[21:25])[0]) & 0x3FFF) + 1
                        b2 = raw[25]
                        h = (((b2 << 10) | struct.unpack("<H", raw[23:25])[0]) & 0x3FFF) + 1
                        if w > 0 and h > 0:
                            return w, h
                elif chunk == b"VP8X":
                    if len(raw) >= 24:
                        w = (raw[23] << 16 | raw[22] << 8 | raw[21]) + 1
                        h = (raw[26] << 16 | raw[25] << 8 | raw[24]) + 1
                        if w > 0 and h > 0:
                            return w, h
    except (struct.error, IndexError):
        pass
    return None, None


def validate_attachment_input(
    input: AttachmentInput,
) -> tuple[bytes, str, int | None, int | None]:
    """Validate a single image input.

    Returns ``(raw_bytes, mime, width, height)``.
    Raises ``AttachmentValidationError`` on any problem.
    """
    path = input.path
    raw_mime = input.mime or ""

    p = Path(path)
    if not p.exists():
        raise AttachmentValidationError(f"File not found: {path}")
    if not p.is_file():
        raise AttachmentValidationError(f"Not a file: {path}")

    size = p.stat().st_size
    if size == 0:
        raise AttachmentValidationError(f"Empty file: {path}")
    if size > _MAX_SOURCE_BYTES:
        raise AttachmentValidationError(
            f"Image too large ({size} bytes, max {_MAX_SOURCE_BYTES})."
        )

    raw = p.read_bytes()

    # 1. Magic-byte check (strict — no extension-only fallback).
    mime = _detect_mime_magic(raw)
    if mime is None:
        raise AttachmentValidationError(
            f"Unsupported image format: {path}"
        )

    if raw_mime and raw_mime != mime:
        raw_mime = mime  # trust magic

    # 2. Dimensions
    width, height = _extract_dimensions(raw, mime)

    return raw, mime, width, height


def count_attachments(inputs: list[AttachmentInput]) -> None:
    """Raise if total input count exceeds the per-prompt limit."""
    if len(inputs) > _MAX_IMAGES_PER_PROMPT:
        raise AttachmentValidationError(
            f"Too many images: {len(inputs)} > {_MAX_IMAGES_PER_PROMPT}"
        )


# ---------------------------------------------------------------------------
# Blob storage
# ---------------------------------------------------------------------------

def _blob_dir(storage_dir: str) -> str:
    """Return the blobs subdirectory path."""
    d = Path(storage_dir) / "blobs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _blob_path(storage_dir: str, blob_hash: str) -> str:
    """Full path to a content-addressed blob."""
    return os.path.join(_blob_dir(storage_dir), blob_hash)


def store_blob(
    storage_dir: str,
    raw: bytes,
    mime: str = "",
) -> AttachmentRef:
    """Store raw bytes, return an immutable ``AttachmentRef``.

    Deduplicates by SHA-256: if the blob already exists it is not rewritten.
    """
    blob_hash = hashlib.sha256(raw).hexdigest()
    bpath = _blob_path(storage_dir, blob_hash)
    if not os.path.exists(bpath):
        # Write atomically via temp file + rename.
        tmp = bpath + ".tmp"
        Path(tmp).write_bytes(raw)
        Path(tmp).rename(bpath)
    return AttachmentRef(
        blob_hash=blob_hash,
        mime=mime or "application/octet-stream",
        size=len(raw),
    )


def store_blob_with_meta(
    storage_dir: str,
    raw: bytes,
    mime: str,
    width: int | None = None,
    height: int | None = None,
) -> AttachmentRef:
    """Store and return an ``AttachmentRef`` with full metadata."""
    blob_hash = hashlib.sha256(raw).hexdigest()
    bpath = _blob_path(storage_dir, blob_hash)
    if not os.path.exists(bpath):
        tmp = bpath + ".tmp"
        Path(tmp).write_bytes(raw)
        Path(tmp).rename(bpath)
    return AttachmentRef(
        blob_hash=blob_hash,
        mime=mime,
        size=len(raw),
        width=width,
        height=height,
    )


def read_blob_bytes(storage_dir: str, blob_hash: str) -> bytes | None:
    """Read raw blob bytes; returns ``None`` when missing."""
    bpath = _blob_path(storage_dir, blob_hash)
    if os.path.exists(bpath):
        return Path(bpath).read_bytes()
    return None


def has_blob(storage_dir: str, blob_hash: str) -> bool:
    """Check if a blob exists."""
    return os.path.exists(_blob_path(storage_dir, blob_hash))


def encode_blob_base64(storage_dir: str, ref: AttachmentRef) -> str:
    """Encode a blob to Base64 for provider payloads."""
    raw = read_blob_bytes(storage_dir, ref.blob_hash)
    if raw is None:
        raise AttachmentStorageError(f"Blob missing: {ref.blob_hash}")
    return base64.b64encode(raw).decode("ascii")


def blob_to_data_url(storage_dir: str, ref: AttachmentRef) -> str:
    """Build a data: URI for OpenAI-compatible ``image_url`` payloads."""
    b64 = encode_blob_base64(storage_dir, ref)
    return f"data:{ref.mime};base64,{b64}"


def remove_blob(storage_dir: str, blob_hash: str) -> bool:
    """Remove a single content-addressed blob by hash.

    Returns ``True`` when the blob existed and was removed, ``False`` otherwise.
    This is the safe primitive for precise rollback — unlike ``orphan_cleanup``,
    it never touches blobs outside the explicitly named hash.
    """
    bpath = _blob_path(storage_dir, blob_hash)
    try:
        if os.path.isfile(bpath):
            os.remove(bpath)
            return True
    except OSError:
        pass
    return False


def orphan_cleanup(storage_dir: str, refs: set[str]) -> int:
    """Remove blobs not referenced by *refs* (set of blob_hash strings).

    Returns the count of removed blobs.
    """
    d = _blob_dir(storage_dir)
    removed = 0
    if not os.path.isdir(d):
        return 0
    for name in os.listdir(d):
        if name.endswith(".tmp"):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass
            removed += 1
            continue
        if name not in refs:
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Import convenience
# ---------------------------------------------------------------------------

def import_image(
    storage_dir: str,
    path: str,
    mime_hint: str = "",
) -> AttachmentRef:
    """End-to-end: validate → store → return ``AttachmentRef``.

    The original *path* is NOT stored on the returned reference — only the
    content-addressed blob hash and MIME survive serialization.
    """
    inp = AttachmentInput(path=path, mime=mime_hint)
    raw, mime, width, height = validate_attachment_input(inp)
    return store_blob_with_meta(
        storage_dir=storage_dir,
        raw=raw,
        mime=mime,
        width=width,
        height=height,
    )
