"""Tests for TUI image path extraction from input text (drag-and-drop scenario)."""

from __future__ import annotations

import struct
from pathlib import Path

from one.modes.tui_mode import _extract_image_paths

# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _make_png(tmp_path: Path, name: str = "img.png") -> Path:
    sig = b"\x89PNG\r\n\x1a\n"
    width = struct.pack(">I", 1)
    height = struct.pack(">I", 1)
    bit_depth_color = b"\x08\x06"
    ihdr_data = width + height + bit_depth_color + b"\x00\x00"
    ihdr_crc = b"\x00\x00\x00\x00"
    ihdr = b"\x00\x00\x00\x0dIHDR" + ihdr_data + ihdr_crc
    idat = b"\x00\x00\x00\x03IDAT\x08\x99c\xfc\xcf\x00\x00\x00\x02\x00\x01"
    idat_crc = b"\x00\x00\x00\x00"
    idat += idat_crc
    iend = b"\x00\x00\x00\x00IEND" + b"\xaeB`\x82"
    p = tmp_path / name
    p.write_bytes(sig + ihdr + idat + iend)
    return p


def _make_jpg(tmp_path: Path, name: str = "img.jpg") -> Path:
    p = tmp_path / name
    p.write_bytes(
        b"\xff\xd8"
        b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00\x43\x00"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b"
        b"\xff\xd9"
    )
    return p


def _make_webp(tmp_path: Path, name: str = "img.webp") -> Path:
    import struct as _struct

    riff = b"RIFF"
    size = _struct.pack("<I", 14)
    webp = b"WEBP"
    vp8 = b"VP8 "
    vp8_size = _struct.pack("<I", 10)
    vp8_data = _struct.pack("<HHb", 1, 1, 0) + b"\x9d\x01\x2a\x01\x00\x9b\x00\xfa\x50\x03"
    end = b"END"
    p = tmp_path / name
    p.write_bytes(riff + size + webp + vp8 + vp8_size + vp8_data + end)
    return p


# ---------------------------------------------------------------------------
# Extraction tests
# ---------------------------------------------------------------------------


class TestExtractImagePaths:
    """Pure function tests — no Textual dependency."""

    def test_no_image_paths_plain_text(self) -> None:
        paths, clean = _extract_image_paths("hello world")
        assert paths == []
        assert clean == "hello world"

    def test_double_quoted_path(self, tmp_path: Path) -> None:
        img = _make_png(tmp_path)
        text = f'look at "{img}"'
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1
        assert paths[0].resolve() == img.resolve()
        assert f'"{img}"' not in clean
        assert clean.strip() == "look at"

    def test_single_quoted_path(self, tmp_path: Path) -> None:
        img = _make_png(tmp_path)
        text = f"look at '{img}'"
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1
        assert paths[0].resolve() == img.resolve()

    def test_angle_bracketed_path(self, tmp_path: Path) -> None:
        img = _make_png(tmp_path)
        text = f"see <{img}>"
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1
        assert paths[0].resolve() == img.resolve()
        assert f"<{img}>" not in clean

    def test_backtick_path(self, tmp_path: Path) -> None:
        img = _make_png(tmp_path)
        text = f"check `{img}`"
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1
        assert paths[0].resolve() == img.resolve()

    def test_unquoted_path(self, tmp_path: Path) -> None:
        img = _make_png(tmp_path)
        text = f"look at {img} please"
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1
        assert paths[0].resolve() == img.resolve()

    def test_multiple_images_same_text(self, tmp_path: Path) -> None:
        png = _make_png(tmp_path, "a.png")
        jpg = _make_jpg(tmp_path, "b.jpg")
        text = f'check "{png}" and {jpg}'
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 2
        resolved = {p.resolve() for p in paths}
        assert resolved == {png.resolve(), jpg.resolve()}

    def test_webp_image(self, tmp_path: Path) -> None:
        img = _make_webp(tmp_path)
        text = f"<{img}>"
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1
        assert paths[0].resolve() == img.resolve()

    def test_deduplication(self, tmp_path: Path) -> None:
        img = _make_png(tmp_path)
        text = f'"{img}" "{img}" "{img}"'
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1

    def test_nonexistent_path_ignored(self) -> None:
        paths, clean = _extract_image_paths("/nonexistent/image.png")
        assert paths == []
        assert clean == "/nonexistent/image.png"

    def test_non_image_ext_ignored(self, tmp_path: Path) -> None:
        p = tmp_path / "test.txt"
        p.write_bytes(b"hello")
        paths, clean = _extract_image_paths(str(p))
        assert paths == []
        assert clean == str(p)

    def test_slash_command_unchanged(self) -> None:
        paths, clean = _extract_image_paths("/model gpt-4")
        assert paths == []
        assert clean == "/model gpt-4"

    def test_normal_text_unchanged(self) -> None:
        paths, clean = _extract_image_paths("What is the capital of France?")
        assert paths == []
        assert clean == "What is the capital of France?"

    def test_image_path_with_spaces_quoted(self, tmp_path: Path) -> None:
        img = _make_png(tmp_path, "my image.png")
        text = f'look at "{img}" now'
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1
        assert paths[0].resolve() == img.resolve()

    def test_jpeg_extension_variants(self, tmp_path: Path) -> None:
        jpg = _make_jpg(tmp_path, "pic.jpeg")
        paths, clean = _extract_image_paths(f"see {jpg}")
        assert len(paths) == 1

    def test_empty_string(self) -> None:
        paths, clean = _extract_image_paths("")
        assert paths == []
        assert clean == ""

    def test_whitespace_only(self) -> None:
        paths, clean = _extract_image_paths("   \n\t  ")
        assert paths == []
        assert clean == ""

    def test_tilde_home_path(self, tmp_path: Path) -> None:
        # Tilde paths that exist should be found.
        # We use the tmp_path which is an absolute path — the test just
        # verifies the logic doesn't crash.
        img = _make_png(tmp_path)
        text = str(img)
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1
        assert paths[0].resolve() == img.resolve()

    def test_path_with_directory_slash_prefix(self, tmp_path: Path) -> None:
        subdir = tmp_path / "sub"
        subdir.mkdir()
        img = _make_png(subdir, "nested.png")
        text = f"look at {img}"
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1

    def test_absolute_image_path_only(self, tmp_path: Path) -> None:
        """Absolute image path as the sole input should be extracted (not treated as slash command)."""
        img = _make_png(tmp_path, "photo.png")
        text = str(img)
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1
        assert paths[0].resolve() == img.resolve()
        assert clean == ""

    def test_absolute_image_path_with_trailing_slash_command(self, tmp_path: Path) -> None:
        """Absolute image path followed by slash command should extract the image and preserve command."""
        img = _make_png(tmp_path, "shot.png")
        text = f"{img} /model gpt-4"
        paths, clean = _extract_image_paths(text)
        assert len(paths) == 1
        assert paths[0].resolve() == img.resolve()
        assert "/model gpt-4" in clean

    def test_absolute_path_nonexistent_unchanged(self) -> None:
        """Non-existent absolute path starting with / should not be extracted."""
        text = "/nonexistent/path/to/image.png"
        paths, clean = _extract_image_paths(text)
        assert paths == []
        assert clean == "/nonexistent/path/to/image.png"

    def test_absolute_path_nonexistent_stays_for_command_dispatch(self) -> None:
        """Non-existent /... path should remain unchanged so slash-command dispatch sees it."""
        text = "/some/weird/command"
        paths, clean = _extract_image_paths(text)
        assert paths == []
        assert clean == "/some/weird/command"
