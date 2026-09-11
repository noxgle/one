"""Explicit image-loading tool for vision-capable models.

Unlike ``read`` (text-only), ``read_image`` validates a PNG/JPEG/WebP file,
imports it into the session's private content-addressed blob store, and
returns a transient image reference for the next provider request.

The original filesystem path is never persisted — only the blob hash and
MIME survive in the transient reference.
"""

from __future__ import annotations

from pathlib import Path

from .common import resolve_to_cwd


def read_image_tool(cwd: str, path: str, storage_dir: str = "") -> dict:
    """Load an image file for vision inspection.

    Semantically this tool only reads a file, but it *does* import the
    image bytes into the session's private content-addressed blob store.
    The blob is transient — it is never persisted to JSONL or emitted
    in events. This tool only inspects workspace files; it never writes,
    edits, or executes anything.

    Args:
        cwd: session working directory (relative paths resolve against it).
        path: absolute or cwd-relative image path.
        storage_dir: session storage dir containing ``blobs/``.

    Returns a dict with ``content`` (text), ``details`` (safe metadata) and
    ``image`` (transient ``AttachmentRef`` dict).  Raises on invalid input.
    """
    from one.core.attachments import (
        AttachmentInput,
        count_attachments,
        import_image,
    )

    if not path or not str(path).strip():
        raise ValueError("read_image requires 'path'")
    cleaned = str(path).strip().strip("\"'")

    # Resolve relative paths against cwd; keep absolute paths as-is.
    resolved = resolve_to_cwd(cleaned, cwd)
    # Sanitise error text: never leak absolute source paths — use just the
    # file name so JSONL / event payloads stay private.
    _label = str(Path(cleaned).name) or "image"
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {_label}")
    if not resolved.is_file():
        raise ValueError(f"Not a file: {_label}")

    if not storage_dir:
        raise ValueError("Image storage is unavailable for this session")

    # Enforce single-image import against the per-prompt limit.
    count_attachments([AttachmentInput(path=str(resolved))])
    ref = import_image(storage_dir, str(resolved))

    # Human-friendly label without leaking the absolute path.
    try:
        label = str(Path(cleaned).name) or "image"
    except Exception:
        label = "image"
    dims = ""
    if ref.width and ref.height:
        dims = f" {ref.width}x{ref.height}"
    text = f"Loaded image: {label} ({ref.mime},{dims} {ref.size} bytes). It will be sent to the vision model on the next step."

    return {
        "content": [{"type": "text", "text": text}],
        "details": {
            "mime": ref.mime,
            "size": ref.size,
            "width": ref.width,
            "height": ref.height,
        },
        "image": {
            "blob_hash": ref.blob_hash,
            "mime": ref.mime,
            "size": ref.size,
            "width": ref.width,
            "height": ref.height,
        },
    }
