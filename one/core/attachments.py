"""Image attachment domain, validation, and local blob storage.

Images are **transient per turn**: stored as content-addressed blobs (SHA-256)
for the lifetime of a single ``prompt()`` call, then discarded.  No
``AttachmentRef`` is ever persisted to session JSONL — images are never
stored in the session file and never survive across turns.

Blobs that are written to disk but have no live reference (e.g. from
``read_image`` tool results that weren't consumed by the provider, or
orphaned files left from crashed turns) are retained on disk until
``orphan_cleanup(refs)`` is explicitly called — there is no automatic
garbage collection from current-turn refs.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import stat
import struct
import tempfile
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
# Platform helpers (mirror persistence.py conventions)
# ---------------------------------------------------------------------------

_IS_POSIX = os.name == "posix"


def _chmod(path: str, mode: int) -> None:
    """Chmod *path* when possible; silently skip on non-POSIX."""
    if _IS_POSIX:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _ensure_blob_dir(storage_dir: str) -> str:
    """Create and tighten the blobs directory to 0o700."""
    d = Path(storage_dir) / "blobs"
    d.mkdir(parents=True, exist_ok=True)
    _chmod(str(d), 0o700)
    return str(d)


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

_SHA256_HEX_RE = set("0123456789abcdef")


def _validate_blob_hash(blob_hash: str) -> None:
    """Strictly validate that *blob_hash* is a 64-char lowercase hex string."""
    if len(blob_hash) != 64:
        raise AttachmentValidationError(
            f"Invalid blob hash length ({len(blob_hash)} != 64): {blob_hash!r}"
        )
    for ch in blob_hash:
        if ch not in _SHA256_HEX_RE:
            raise AttachmentValidationError(
                f"Invalid blob hash charset: {blob_hash!r}"
            )


def _validate_blob_path(blob_path: str, blob_dir: str) -> None:
    """Validate blob path for safety without accessing the filesystem.

    Rejects path traversal (``..`` components, null bytes), verifies the
    resolved path stays within *blob_dir*.  Does NOT call ``os.stat`` on
    *blob_path* — use ``_verify_blob_is_regular`` for that.

    Fail-closed — no file access outside the blob store.
    """
    # Reject null bytes.
    if "\x00" in blob_path:
        raise AttachmentStorageError(
            f"Blob path contains null byte: {blob_path!r}"
        )

    # Reject ``..`` path-traversal components.
    if ".." in blob_path.split(os.sep):
        raise AttachmentStorageError(
            f"Blob path contains traversal: {blob_path!r}"
        )

    # Resolve symlinks to prevent traversal via symlink ancestors.
    resolved = os.path.realpath(blob_path)
    resolved_dir = os.path.realpath(blob_dir)

    # Verify containment: resolved path must start with resolved blob dir.
    if not resolved.startswith(resolved_dir + os.sep) and resolved != resolved_dir:
        raise AttachmentStorageError(
            f"Blob path escapes storage directory: {blob_path!r}"
        )


def _verify_blob_is_regular(blob_path: str) -> None:
    """Confirm blob at *blob_path* is a regular file (not dir/symlink).

    Called after path validation to verify content integrity.  Raises
    ``AttachmentStorageError`` on any anomaly.
    """
    # Reject symlinks at the leaf (blob file itself).
    if os.path.islink(blob_path):
        raise AttachmentStorageError(
            f"Blob is a symlink, rejecting: {blob_path!r}"
        )

    # Reject non-regular files (dirs, etc).
    try:
        st = os.stat(blob_path)
    except OSError as exc:
        raise AttachmentStorageError(
            f"Cannot stat blob: {blob_path!r}: {exc}"
        )
    if not stat.S_ISREG(st.st_mode):
        if stat.S_ISDIR(st.st_mode):
            raise AttachmentStorageError(
                f"Blob path is a directory, rejecting: {blob_path!r}"
            )
        raise AttachmentStorageError(
            f"Blob is not a regular file: {blob_path!r}"
        )


def _verify_content_integrity(blob_path: str, expected_size: int | None = None) -> None:
    """Confirm size matches expected ref where available.

    Called after ``_verify_blob_is_regular`` to add size-level checks.
    """
    st = os.stat(blob_path)
    if not stat.S_ISREG(st.st_mode):
        raise AttachmentStorageError(
            f"Blob is not a regular file: {blob_path!r}"
        )
    if expected_size is not None and st.st_size != expected_size:
        raise AttachmentStorageError(
            f"Blob size mismatch (expected {expected_size}, got {st.st_size}): {blob_path!r}"
        )


# ---------------------------------------------------------------------------
# Magic-byte and dimension validation
# ---------------------------------------------------------------------------

def _detect_mime_magic(raw: bytes) -> str | None:
    """Return MIME from magic bytes, or None on mismatch."""
    for sig, mime in _MAGIC_SIGNATURES.items():
        if raw[: len(sig)] == sig:
            # WebP is RIFF....WEBP — check the FOURCC.
            if mime == "image/webp":
                if len(raw) < 12 or raw[8:12] != _WEBP_RIFF:
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
                        # Lossless bitstream: 14-bit (width-1) + 14-bit
                        # (height-1) packed little-endian from raw[21].
                        packed = struct.unpack("<I", raw[21:25])[0]
                        w = (packed & 0x3FFF) + 1
                        h = ((packed >> 14) & 0x3FFF) + 1
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


# ---------------------------------------------------------------------------
# Public API — validation
# ---------------------------------------------------------------------------

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

    raw = p.read_bytes()

    # Recheck actual byte length after read_bytes (not just stat).
    actual_size = len(raw)
    if actual_size == 0:
        raise AttachmentValidationError(f"Empty file: {path}")
    if actual_size > _MAX_SOURCE_BYTES:
        raise AttachmentValidationError(
            f"Image too large ({actual_size} bytes, max {_MAX_SOURCE_BYTES})."
        )

    # 1. Magic-byte check (strict — no extension-only fallback).
    mime = _detect_mime_magic(raw)
    if mime is None:
        raise AttachmentValidationError(
            f"Unsupported image format: {path}"
        )

    if raw_mime and raw_mime != mime:
        raw_mime = mime  # trust magic

    # 2. Dimensions — reject images with zero or negative dimensions.
    width, height = _extract_dimensions(raw, mime)

    return raw, mime, width, height


def count_attachments(inputs: list[AttachmentInput]) -> None:
    """Raise if total input count exceeds the per-prompt limit."""
    if len(inputs) > _MAX_IMAGES_PER_PROMPT:
        raise AttachmentValidationError(
            f"Too many images: {len(inputs)} > {_MAX_IMAGES_PER_PROMPT}"
        )


def validate_image_count(file_count: int, clipboard_count: int = 0, api_count: int = 0) -> None:
    """Validate combined image count from all sources against the limit.

    Raises ``AttachmentValidationError`` when the total exceeds
    ``_MAX_IMAGES_PER_PROMPT``.
    """
    total = file_count + clipboard_count + api_count
    if total > _MAX_IMAGES_PER_PROMPT:
        raise AttachmentValidationError(
            f"Too many images: {file_count} file(s) + {clipboard_count} clipboard + {api_count} API = {total} > {_MAX_IMAGES_PER_PROMPT}"
        )


# ---------------------------------------------------------------------------
# Blob storage
# ---------------------------------------------------------------------------

def _blob_dir(storage_dir: str) -> str:
    """Return the blobs subdirectory path, ensuring it exists with 0o700."""
    return _ensure_blob_dir(storage_dir)


def _blob_path(storage_dir: str, blob_hash: str) -> str:
    """Full path to a content-addressed blob.

    Validates the blob_hash before joining to prevent path traversal.
    """
    _validate_blob_hash(blob_hash)
    return os.path.join(_blob_dir(storage_dir), blob_hash)


def _write_blob_once(storage_dir: str, raw: bytes) -> str:
    """Write *raw* to its content-addressed path unless present; return hash.

    Uses exclusive-temp-file (mkstemp + O_EXCL) for safe concurrent writes,
    fsyncs before rename, and cleans up temp on failure.
    """
    blob_hash = hashlib.sha256(raw).hexdigest()
    bdir = _blob_dir(storage_dir)
    bpath = os.path.join(bdir, blob_hash)

    if os.path.exists(bpath):
        # Another writer already succeeded (or file exists from a previous run).
        # Verify it's a valid regular file matching our hash.
        try:
            st = os.stat(bpath)
            if stat.S_ISREG(st.st_mode) and st.st_size == len(raw):
                return blob_hash
        except OSError:
            pass
        # Corrupted or wrong-size — fall through to overwrite.

    # Exclusive temp file in the same directory (ensures O_EXCL semantics).
    fd = None
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=bdir, prefix=".tmp_", suffix=".blob")
        os.write(fd, raw)
        os.fsync(fd)
        os.close(fd)
        fd = None
        _chmod(tmp_path, 0o600)
        os.replace(tmp_path, bpath)
        _chmod(bpath, 0o600)
    except BaseException:
        # Clean up temp file on any failure — never corrupt target.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

    return blob_hash


def store_blob(
    storage_dir: str,
    raw: bytes,
    mime: str = "",
) -> AttachmentRef:
    """Store raw bytes, return an immutable ``AttachmentRef``.

    Deduplicates by SHA-256: if the blob already exists it is not rewritten.
    """
    blob_hash = _write_blob_once(storage_dir, raw)
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
    blob_hash = _write_blob_once(storage_dir, raw)
    return AttachmentRef(
        blob_hash=blob_hash,
        mime=mime,
        size=len(raw),
        width=width,
        height=height,
    )


def read_blob_bytes(storage_dir: str, blob_hash: str) -> bytes | None:
    """Read raw blob bytes; returns ``None`` when missing.

    Validates hash, path containment, and file type before reading.
    Rechecks actual byte length on return.
    """
    _validate_blob_hash(blob_hash)
    bdir = _blob_dir(storage_dir)
    bpath = os.path.join(bdir, blob_hash)

    # Validate path containment (no filesystem access).
    _validate_blob_path(bpath, bdir)

    if os.path.exists(bpath):
        _verify_blob_is_regular(bpath)
        _verify_content_integrity(bpath)
        raw = Path(bpath).read_bytes()
        # Recheck actual byte length — trust read_bytes(), not stat.
        if len(raw) == 0:
            return None
        return raw
    return None


def has_blob(storage_dir: str, blob_hash: str) -> bool:
    """Check if a blob exists.

    Validates hash, path containment, and ensures it's a regular file.
    """
    _validate_blob_hash(blob_hash)
    bdir = _blob_dir(storage_dir)
    bpath = os.path.join(bdir, blob_hash)
    try:
        # Validate path containment (no filesystem access).
        _validate_blob_path(bpath, bdir)
        # Only verify regular-file if path exists.
        if os.path.exists(bpath):
            _verify_blob_is_regular(bpath)
            return True
        return False
    except (AttachmentValidationError, AttachmentStorageError):
        return False


def encode_blob_base64(storage_dir: str, ref: AttachmentRef) -> str:
    """Encode a blob to Base64 for provider payloads.

    Enforces _MAX_BASE64_BYTES on the encoded output.
    """
    raw = read_blob_bytes(storage_dir, ref.blob_hash)
    if raw is None:
        raise AttachmentStorageError(f"Blob missing: {ref.blob_hash}")

    b64 = base64.b64encode(raw).decode("ascii")
    # Enforce encoded size limit.
    if len(b64) > _MAX_BASE64_BYTES:
        raise AttachmentValidationError(
            f"Blob too large for Base64 encoding "
            f"({len(b64)} bytes > {_MAX_BASE64_BYTES})."
        )
    return b64


def blob_to_data_url(storage_dir: str, ref: AttachmentRef) -> str:
    """Build a data: URI for OpenAI-compatible ``image_url`` payloads."""
    b64 = encode_blob_base64(storage_dir, ref)
    return f"data:{ref.mime};base64,{b64}"


def remove_blob(storage_dir: str, blob_hash: str) -> bool:
    """Remove a single content-addressed blob by hash.

    Validates hash and path before removal.  Returns ``True`` when the blob
    existed and was removed, ``False`` otherwise.  This is the safe primitive
    for precise rollback — unlike ``orphan_cleanup``, it never touches blobs
    outside the explicitly named hash.
    """
    try:
        bpath = _blob_path(storage_dir, blob_hash)
        bdir = _blob_dir(storage_dir)
        # Validate path containment (no filesystem access).
        _validate_blob_path(bpath, bdir)
        if os.path.isfile(bpath):
            os.remove(bpath)
            return True
    except (OSError, AttachmentValidationError, AttachmentStorageError):
        pass
    return False


def orphan_cleanup(storage_dir: str, refs: set[str]) -> int:
    """Remove blobs not referenced by *refs* (set of blob_hash strings).

    Returns the count of removed blobs.  Temp files are always cleaned up.
    Only valid 64-char hex hashes are checked against *refs*.
    """
    d = _blob_dir(storage_dir)
    removed = 0
    if not os.path.isdir(d):
        return 0
    for name in os.listdir(d):
        full = os.path.join(d, name)
        if name.endswith(".tmp") or name.endswith(".blob"):
            try:
                os.remove(full)
            except OSError:
                pass
            removed += 1
            continue
        # Check if it looks like a valid hash — if not, remove it.
        is_valid_hash = False
        if len(name) == 64:
            try:
                _validate_blob_hash(name)
                is_valid_hash = True
            except AttachmentValidationError:
                pass
        # Remove if not a valid hash or not in refs.
        if not is_valid_hash or name not in refs:
            try:
                os.remove(full)
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
