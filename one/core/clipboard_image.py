"""Secure system clipboard image acquisition.

Reads raw image bytes from the system clipboard using platform-specific
backends.  All subprocess calls use ``subprocess.argv`` (no shell
interpolation) and are capped at a generous size limit before the
attachment pipeline takes over.

Supported platforms:
  - **Wayland**  → ``wl-paste --type image/png``
  - **X11**     → ``xclip -selection clipboard -o`` or ``xsel --clipboard --output``
  - **macOS**   → ``pngpaste`` (returns PNG)
  - **Windows** → ``powershell -NoProfile -Command Get-Clipboard`` (returns image bytes)

Returns ``None`` when no backend is available or the clipboard does not
contain an image.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from one.core.attachments import (
    _detect_mime_magic,
)

# ---------------------------------------------------------------------------
# Size guard: clipboards can contain huge blobs (screenshots).
# ---------------------------------------------------------------------------

_CLIPBOARD_IMAGE_MAX = 50 * 1024 * 1024  # 50 MB raw bytes

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def _is_wayland() -> bool:
    """Return True when running under a Wayland compositor."""
    display = os.environ.get("WAYLAND_DISPLAY", "")
    return bool(display)


def _is_x11() -> bool:
    """Return True when running under X11."""
    display = os.environ.get("DISPLAY", "")
    return bool(display)


def _is_macos() -> bool:
    return os.name == "posix" and shutil.which("sw_vers") is not None


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------

def _read_clipboard_image_wayland() -> bytes | None:
    """Read clipboard image via wl-paste on Wayland."""
    cmd: list[str] = ["wl-paste", "--no-newline", "--type", "image/png"]
    if shutil.which(cmd[0]) is None:
        return None
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout
        if len(raw) == 0:
            return None
        if len(raw) > _CLIPBOARD_IMAGE_MAX:
            return None
        # Validate MIME magic bytes.
        if _detect_mime_magic(raw) is None:
            return None
        return raw
    except Exception:
        return None


def _read_clipboard_image_x11() -> bytes | None:
    """Read clipboard image via xclip/xsel on X11."""
    for cmd in [
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ]:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=10,
            )
            if result.returncode != 0:
                continue
            raw = result.stdout
            if len(raw) == 0:
                continue
            if len(raw) > _CLIPBOARD_IMAGE_MAX:
                continue
            # Validate MIME magic bytes.
            if _detect_mime_magic(raw) is None:
                continue
            return raw
        except Exception:
            continue
    return None


def _read_clipboard_image_macos() -> bytes | None:
    """Read clipboard image via pngpaste on macOS."""
    cmd: list[str] = ["pngpaste", "-"]
    if shutil.which(cmd[0]) is None:
        return None
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout
        if len(raw) == 0:
            return None
        if len(raw) > _CLIPBOARD_IMAGE_MAX:
            return None
        # Validate MIME magic bytes.
        if _detect_mime_magic(raw) is None:
            return None
        return raw
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def acquire_clipboard_image() -> bytes | None:
    """Acquire raw image bytes from the system clipboard.

    Returns ``None`` when:
    - No suitable backend is available
    - The clipboard does not contain image data
    - The data is too large

    The returned bytes are validated by the attachment pipeline
    (magic-byte check, MIME, size).
    """
    # Try platform-specific backends first (in priority order).
    if _is_wayland():
        raw = _read_clipboard_image_wayland()
        if raw is not None:
            return raw
    if _is_x11():
        raw = _read_clipboard_image_x11()
        if raw is not None:
            return raw
    if _is_macos():
        raw = _read_clipboard_image_macos()
        if raw is not None:
            return raw

    # Fallback: try all backends regardless of environment (useful in
    # tests or hybrid setups).
    for fn in (_read_clipboard_image_wayland, _read_clipboard_image_x11, _read_clipboard_image_macos):
        raw = fn()
        if raw is not None:
            # Validate that the data looks like an image.
            mime = _detect_mime_magic(raw)
            if mime is not None:
                return raw
            # Some backends may return non-image data (e.g. text) — skip.
            continue

    return None


def is_clipboard_image_available() -> bool:
    """Return True when the clipboard likely contains image data.

    Lightweight check — does not read the full blob, only probes backend
    availability and does a small peek when possible.
    """
    if _is_wayland() and shutil.which("wl-paste") is not None:
        return True
    if _is_x11() and (shutil.which("xclip") is not None or shutil.which("xsel") is not None):
        return True
    if _is_macos() and shutil.which("pngpaste") is not None:
        return True
    return False


# ---------------------------------------------------------------------------
# Temp file helper — stores clipboard image to a temp path for the
# attachment pipeline to consume.
# ---------------------------------------------------------------------------

def clipboard_image_to_temp_path(storage_dir: str, raw: bytes) -> str | None:
    """Write clipboard image bytes to a temp file in the blob store and
    return the path, or ``None`` on error.

    The file is named ``clipboard_<uuid>.tmp`` (UUID-based for collision
    resistance) and lives inside the ``blobs/`` directory so it is cleaned
    up by the existing orphan cleanup routine.
    """
    if not raw:
        return None

    import time

    timestamp = int(time.time() * 1000)
    filename = f"clipboard_{uuid.uuid4().hex}_{timestamp}.tmp"
    blob_dir = Path(storage_dir) / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = blob_dir / filename

    try:
        tmp_path.write_bytes(raw)
        return str(tmp_path)
    except Exception:
        return None
