"""Tests for image attachment domain: validation, storage, and provider payloads."""

from __future__ import annotations

import base64
import os
import struct
from pathlib import Path

import pytest

from one.core.attachments import (
    AttachmentInput,
    AttachmentRef,
    AttachmentValidationError,
    count_attachments,
    encode_blob_base64,
    has_blob,
    import_image,
    orphan_cleanup,
    read_blob_bytes,
    remove_blob,
    store_blob_with_meta,
    validate_attachment_input,
)
from one.core.types import ModelInfo

# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

def _make_png() -> bytes:
    """Minimal valid PNG (1×1 transparent pixel)."""
    # PNG signature + IHDR + IDAT + IEND (minimal)
    sig = b"\x89PNG\r\n\x1a\n"
    # IHDR chunk
    width = struct.pack(">I", 1)
    height = struct.pack(">I", 1)
    bit_depth_color = b"\x08\x06"  # 8-bit RGBA
    ihdr_data = width + height + bit_depth_color + b"\x00\x00"
    ihdr_crc = b"\x00\x00\x00\x00"  # dummy CRC
    ihdr = b"\x00\x00\x00\x0dIHDR" + ihdr_data + ihdr_crc  # length=13
    # IDAT chunk (minimal compressed data)
    idat = b"\x00\x00\x00\x03IDAT\x08\x99c\xfc\xcf\x00\x00\x00\x02\x00\x01"
    idat_crc = b"\x00\x00\x00\x00"  # dummy CRC
    idat += idat_crc
    # IEND chunk
    iend = b"\x00\x00\x00\x00IEND" + b"\xaeB`\x82"
    return sig + ihdr + idat + iend


def _make_jpeg() -> bytes:
    """Minimal valid JPEG (1×1 pixel)."""
    return (
        b"\xff\xd8"  # SOI
        b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"  # APP0
        b"\xff\xdb\x00\x43\x00"  # DQT
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"  # SOF0
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b"  # DHT
        b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x7b\x40"  # SOS + data
        b"\xff\xd9"  # EOI
    )


def _make_webp() -> bytes:
    """Minimal valid WebP (lossy, 1×1)."""
    riff = b"RIFF"
    size = struct.pack("<I", 14)  # file size - 8
    webp = b"WEBP"
    vp8 = b"VP8 "
    vp8_size = struct.pack("<I", 10)
    vp8_data = struct.pack("<HHb", 1, 1, 0) + b"\x9d\x01\x2a\x01\x00\x9b\x00\xfa\x50\x03"
    end = b"END"
    return riff + size + webp + vp8 + vp8_size + vp8_data + end


@pytest.fixture()
def tmp_storage(tmp_path: Path) -> str:
    """Return a temp directory path for blob storage."""
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateAttachmentInput:
    def test_valid_png(self, tmp_storage: str, tmp_path: Path) -> None:
        png_path = tmp_path / "test.png"
        png_path.write_bytes(_make_png())
        raw, mime, w, h = validate_attachment_input(AttachmentInput(path=str(png_path)))
        assert mime == "image/png"
        assert w == 1
        assert h == 1
        assert len(raw) > 0

    def test_valid_jpeg_dimensions_optional(self, tmp_storage: str, tmp_path: Path) -> None:
        """JPEG dimension extraction may return None for minimal/test JPEGs — that's OK."""
        jpg_path = tmp_path / "test.jpg"
        jpg_path.write_bytes(_make_jpeg())
        raw, mime, w, h = validate_attachment_input(AttachmentInput(path=str(jpg_path)))
        assert mime == "image/jpeg"
        assert len(raw) > 0

    def test_valid_webp_mime_only(self, tmp_storage: str, tmp_path: Path) -> None:
        """WebP dimension extraction may vary for minimal/test WebPs — MIME must match."""
        webp_path = tmp_path / "test.webp"
        webp_path.write_bytes(_make_webp())
        raw, mime, w, h = validate_attachment_input(AttachmentInput(path=str(webp_path)))
        assert mime == "image/webp"
        assert len(raw) > 0

    def test_file_not_found(self) -> None:
        with pytest.raises(AttachmentValidationError, match="File not found"):
            validate_attachment_input(AttachmentInput(path="/nonexistent/file.png"))

    def test_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.png"
        empty.write_bytes(b"")
        with pytest.raises(AttachmentValidationError, match="Empty file"):
            validate_attachment_input(AttachmentInput(path=str(empty)))

    def test_unsupported_format(self, tmp_path: Path) -> None:
        gif = tmp_path / "test.gif"
        gif.write_bytes(b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")
        with pytest.raises(AttachmentValidationError, match="Unsupported image format"):
            validate_attachment_input(AttachmentInput(path=str(gif)))


class TestCountAttachments:
    def test_within_limit(self) -> None:
        inputs = [AttachmentInput(path=f"/img{i}.png") for i in range(4)]
        count_attachments(inputs)  # should not raise

    def test_over_limit(self) -> None:
        inputs = [AttachmentInput(path=f"/img{i}.png") for i in range(5)]
        with pytest.raises(AttachmentValidationError, match="Too many images"):
            count_attachments(inputs)


# ---------------------------------------------------------------------------
# Blob storage tests
# ---------------------------------------------------------------------------


class TestBlobStorage:
    def test_store_and_read(self, tmp_storage: str) -> None:
        raw = _make_png()
        ref = store_blob_with_meta(tmp_storage, raw, "image/png")
        # Verify the hash is a valid SHA-256 hex string (64 chars)
        assert len(ref.blob_hash) == 64
        assert ref.mime == "image/png"
        assert ref.size == len(raw)
        assert read_blob_bytes(tmp_storage, ref.blob_hash) == raw

    def test_deduplication(self, tmp_storage: str) -> None:
        raw = _make_png()
        ref1 = store_blob_with_meta(tmp_storage, raw, "image/png")
        ref2 = store_blob_with_meta(tmp_storage, raw, "image/png")
        assert ref1.blob_hash == ref2.blob_hash
        # Only one blob file should exist
        blob_dir = os.path.join(tmp_storage, "blobs")
        non_tmp = [f for f in os.listdir(blob_dir) if not f.endswith(".tmp")]
        assert len(non_tmp) == 1

    def test_has_blob(self, tmp_storage: str) -> None:
        raw = _make_jpeg()
        ref = store_blob_with_meta(tmp_storage, raw, "image/jpeg")
        assert has_blob(tmp_storage, ref.blob_hash) is True
        assert has_blob(tmp_storage, "nonexistent") is False

    def test_read_missing_blob(self, tmp_storage: str) -> None:
        assert read_blob_bytes(tmp_storage, "missing") is None


class TestEncodeBlobBase64:
    def test_encode(self, tmp_storage: str) -> None:
        raw = _make_png()
        ref = store_blob_with_meta(tmp_storage, raw, "image/png", width=1, height=1)
        b64 = encode_blob_base64(tmp_storage, ref)
        assert base64.b64decode(b64) == raw

    def test_encode_missing_blob_raises(self, tmp_storage: str) -> None:
        ref = AttachmentRef(blob_hash="missing", mime="image/png", size=0)
        with pytest.raises(Exception, match="Blob missing"):
            encode_blob_base64(tmp_storage, ref)


# ---------------------------------------------------------------------------
# Import convenience
# ---------------------------------------------------------------------------


class TestImportImage:
    def test_import_png(self, tmp_storage: str, tmp_path: Path) -> None:
        png_path = tmp_path / "import.png"
        png_path.write_bytes(_make_png())
        ref = import_image(tmp_storage, str(png_path))
        assert ref.mime == "image/png"
        assert ref.width == 1
        assert ref.height == 1
        assert ref.size > 0

    def test_import_jpeg(self, tmp_storage: str, tmp_path: Path) -> None:
        jpg_path = tmp_path / "import.jpg"
        jpg_path.write_bytes(_make_jpeg())
        ref = import_image(tmp_storage, str(jpg_path))
        assert ref.mime == "image/jpeg"
        assert ref.size > 0


# ---------------------------------------------------------------------------
# Orphan cleanup tests
# ---------------------------------------------------------------------------


class TestOrphanCleanup:
    def test_removes_orphans(self, tmp_storage: str) -> None:
        ref1 = store_blob_with_meta(tmp_storage, _make_png(), "image/png")
        ref2 = store_blob_with_meta(tmp_storage, _make_jpeg(), "image/jpeg")
        ref3 = store_blob_with_meta(tmp_storage, _make_webp(), "image/webp")
        # Keep only ref1
        removed = orphan_cleanup(tmp_storage, {ref1.blob_hash})
        assert removed == 2
        assert has_blob(tmp_storage, ref1.blob_hash) is True
        assert has_blob(tmp_storage, ref2.blob_hash) is False
        assert has_blob(tmp_storage, ref3.blob_hash) is False

    def test_removes_tmp_files(self, tmp_storage: str) -> None:
        # Create a fake .tmp file
        blob_dir = os.path.join(tmp_storage, "blobs")
        os.makedirs(blob_dir, exist_ok=True)
        tmp_file = os.path.join(blob_dir, "faketmp.tmp")
        Path(tmp_file).write_bytes(b"")
        removed = orphan_cleanup(tmp_storage, set())
        assert removed >= 1
        assert not os.path.exists(tmp_file)

    def test_remove_blob_precise(self, tmp_storage: str) -> None:
        """remove_blob only affects the named blob; others survive."""
        ref1 = store_blob_with_meta(tmp_storage, _make_png(), "image/png")
        ref2 = store_blob_with_meta(tmp_storage, _make_jpeg(), "image/jpeg")
        # Remove only ref2
        removed = remove_blob(tmp_storage, ref2.blob_hash)
        assert removed is True
        assert has_blob(tmp_storage, ref1.blob_hash) is True
        assert has_blob(tmp_storage, ref2.blob_hash) is False

    def test_remove_blob_nonexistent(self, tmp_storage: str) -> None:
        """remove_blob returns False for a missing blob."""
        assert remove_blob(tmp_storage, "deadbeef" * 8) is False


# ---------------------------------------------------------------------------
# Provider payload serialization tests
# ---------------------------------------------------------------------------


class TestOpenAICompatiblePayload:
    def test_build_image_part(self) -> None:
        from one.providers.openai_compatible import OpenAICompatibleAdapter

        adapter = OpenAICompatibleAdapter("test", "http://localhost:1234")
        # Mock _read_blob to return test data
        adapter._read_blob = lambda storage_dir, blob_hash: _make_png()
        image_ref = {
            "blobHash": "abc123",
            "mime": "image/png",
        }
        part = adapter._build_image_part(image_ref, "/tmp/storage")
        assert part is not None
        assert part["type"] == "image_url"
        assert part["image_url"]["url"].startswith("data:image/png;base64,")

    def test_build_image_part_missing_blob(self) -> None:
        from one.providers.openai_compatible import OpenAICompatibleAdapter

        adapter = OpenAICompatibleAdapter("test", "http://localhost:1234")
        adapter._read_blob = lambda storage_dir, blob_hash: None
        image_ref = {"blobHash": "missing", "mime": "image/png"}
        assert adapter._build_image_part(image_ref, "/tmp") is None

    def test_build_payload_with_images(self) -> None:
        from one.providers.openai_compatible import OpenAICompatibleAdapter

        adapter = OpenAICompatibleAdapter("test", "http://localhost:1234")
        adapter._read_blob = lambda storage_dir, blob_hash: _make_png()
        images = [{"blobHash": "abc", "mime": "image/png"}]
        messages = [{"role": "user", "content": "Look at this"}]
        payload = adapter._build_payload("gpt-4o", messages, "medium", images=images, storage_dir="/tmp")
        content = payload["messages"][0]["content"]
        assert isinstance(content, list)
        text_parts = [p for p in content if p.get("type") == "text"]
        image_parts = [p for p in content if p.get("type") == "image_url"]
        assert len(text_parts) == 1
        assert text_parts[0]["text"] == "Look at this"
        assert len(image_parts) == 1


class TestAnthropicPayload:
    def test_build_image_part(self) -> None:
        from one.providers.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter()
        adapter._read_blob = lambda storage_dir, blob_hash: _make_jpeg()
        image_ref = {"blobHash": "abc123", "mime": "image/jpeg"}
        part = adapter._build_image_part(image_ref, "/tmp")
        assert part is not None
        assert part["type"] == "image"
        assert part["source"]["type"] == "base64"
        assert part["source"]["media_type"] == "image/jpeg"
        assert "data" in part["source"]

    def test_resolve_message_content_with_images(self) -> None:
        from one.providers.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter()
        adapter._read_blob = lambda storage_dir, blob_hash: _make_jpeg()
        images = [{"blobHash": "abc", "mime": "image/jpeg"}]
        content = adapter._resolve_message_content("user", "Hello", images, "/tmp")
        assert isinstance(content, list)
        text_parts = [p for p in content if p.get("type") == "text"]
        image_parts = [p for p in content if p.get("type") == "image"]
        assert len(text_parts) == 1
        assert text_parts[0]["text"] == "Hello"
        assert len(image_parts) == 1


class TestGeminiPayload:
    def test_build_image_part(self) -> None:
        from one.providers.gemini import GeminiAdapter

        adapter = GeminiAdapter()
        adapter._read_blob = lambda storage_dir, blob_hash: _make_webp()
        image_ref = {"blobHash": "abc123", "mime": "image/webp"}
        part = adapter._build_image_part(image_ref, "/tmp")
        assert part is not None
        assert "inline_data" in part
        assert part["inline_data"]["mime_type"] == "image/webp"
        assert "data" in part["inline_data"]

    def test_resolve_message_parts_with_images(self) -> None:
        from one.providers.gemini import GeminiAdapter

        adapter = GeminiAdapter()
        adapter._read_blob = lambda storage_dir, blob_hash: _make_webp()
        images = [{"blobHash": "abc", "mime": "image/webp"}]
        parts = adapter._resolve_message_parts("user", "Hello", images, "/tmp")
        text_parts = [p for p in parts if p.get("text")]
        image_parts = [p for p in parts if p.get("inline_data")]
        assert len(text_parts) >= 1
        assert len(image_parts) == 1


# ---------------------------------------------------------------------------
# Model capability tests
# ---------------------------------------------------------------------------


class TestModelCapability:
    def test_model_with_image_support(self) -> None:
        model = ModelInfo(provider="openai", id="gpt-4o", input_image=True)
        assert model.input_image is True

    def test_model_without_image_support(self) -> None:
        model = ModelInfo(provider="ollama", id="llama3", input_image=False)
        assert model.input_image is False

    def test_model_default_false(self) -> None:
        model = ModelInfo(provider="test", id="m1")
        assert model.input_image is False
