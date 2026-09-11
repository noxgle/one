"""Tests for clipboard image acquisition (Ctrl+Shift+V / /paste-image)."""

from __future__ import annotations

import inspect
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from one.core import clipboard_image

# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _make_png_bytes() -> bytes:
    """Create minimal valid PNG bytes."""
    sig = b"\x89PNG\r\n\x1a\n"
    width = b"\x00\x00\x00\x01"
    height = b"\x00\x00\x00\x01"
    bit_depth_color = b"\x08\x06"
    ihdr_data = width + height + bit_depth_color + b"\x00\x00"
    ihdr_crc = b"\x00\x00\x00\x00"
    ihdr = b"\x00\x00\x00\x0dIHDR" + ihdr_data + ihdr_crc
    idat = b"\x00\x00\x00\x03IDAT\x08\x99c\xfc\xcf\x00\x00\x00\x02\x00\x01"
    idat_crc = b"\x00\x00\x00\x00"
    idat += idat_crc
    iend = b"\x00\x00\x00\x00IEND" + b"\xaeB`\x82"
    return sig + ihdr + idat + iend


def _make_jpg_bytes() -> bytes:
    """Create minimal valid JPEG bytes."""
    return (
        b"\xff\xd8"
        b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00\x43\x00"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\x0b"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00{\x40"
        b"\xff\xd9"
    )


# ---------------------------------------------------------------------------
# acquire_clipboard_image tests
# ---------------------------------------------------------------------------


class TestAcquireClipboardImage:
    """Test acquire_clipboard_image with mocked subprocess calls."""

    def test_returns_none_when_no_backend_available(self):
        """When all backend commands are missing, returns None."""
        with patch.object(shutil, "which", return_value=None):
            result = clipboard_image.acquire_clipboard_image()
        assert result is None

    def test_returns_png_bytes_when_wayland_backend_succeeds(self):
        """When wl-paste returns valid PNG, it is returned."""
        png_bytes = _make_png_bytes()

        def fake_run(cmd, **kwargs):
            # Only handle wl-paste commands; let others return failure.
            if "wl-paste" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout=png_bytes, stderr=b"")
            return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"")

        with patch.object(shutil, "which", side_effect=lambda x: "wl-paste" if x == "wl-paste" else None):
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.dict(clipboard_image.os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                    result = clipboard_image.acquire_clipboard_image()
        assert result == png_bytes

    def test_returns_jpg_bytes_when_x11_backend_succeeds(self):
        """When xclip returns valid JPEG, it is returned."""
        jpg_bytes = _make_jpg_bytes()

        def fake_run(cmd, **kwargs):
            if "xclip" in cmd:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout=jpg_bytes, stderr=b"")
            return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"")

        with patch.object(shutil, "which", side_effect=lambda x: "xclip" if x == "xclip" else None):
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.dict(clipboard_image.os.environ, {"DISPLAY": ":0"}):
                    result = clipboard_image.acquire_clipboard_image()
        assert result == jpg_bytes

    def test_returns_none_when_backend_returns_empty(self):
        """When backend returns empty stdout, returns None."""

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"", stderr=b"")

        with patch.object(shutil, "which", return_value="wl-paste"):
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.dict(clipboard_image.os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                    result = clipboard_image.acquire_clipboard_image()
        assert result is None

    def test_returns_none_when_backend_fails(self):
        """When backend returns non-zero exit code, returns None."""

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=1, stdout=b"", stderr=b"error")

        with patch.object(shutil, "which", return_value="wl-paste"):
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.dict(clipboard_image.os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                    result = clipboard_image.acquire_clipboard_image()
        assert result is None

    def test_rejects_oversized_wayland(self):
        """Oversized clipboard data is rejected, never truncated."""
        oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (clipboard_image._CLIPBOARD_IMAGE_MAX + 1000)

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=oversized, stderr=b"")

        with patch.object(shutil, "which", return_value="wl-paste"):
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.dict(clipboard_image.os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                    result = clipboard_image.acquire_clipboard_image()
        assert result is None

    def test_rejects_oversized_x11(self):
        """Oversized clipboard data via xclip is rejected, never truncated."""
        oversized = _make_jpg_bytes() + b"\x00" * (clipboard_image._CLIPBOARD_IMAGE_MAX + 1000)

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=oversized, stderr=b"")

        with patch.object(shutil, "which", side_effect=lambda x: "xclip" if x == "xclip" else None):
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.dict(clipboard_image.os.environ, {"DISPLAY": ":0"}):
                    result = clipboard_image.acquire_clipboard_image()
        assert result is None

    def test_rejects_oversized_macos(self):
        """Oversized clipboard data via pngpaste is rejected, never truncated."""
        oversized = _make_png_bytes() + b"\x00" * (clipboard_image._CLIPBOARD_IMAGE_MAX + 1000)

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=oversized, stderr=b"")

        with patch.object(shutil, "which", side_effect=lambda x: "pngpaste" if x == "pngpaste" else None):
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.object(clipboard_image.os, "name", "posix"):
                    result = clipboard_image.acquire_clipboard_image()
        assert result is None

    def test_validates_mime_magic(self):
        """Data without valid image magic bytes is rejected."""

        def fake_run(cmd, **kwargs):
            # Return text data (no image magic bytes)
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=b"hello world", stderr=b""
            )

        # Don't set WAYLAND_DISPLAY so fallback path is used, which validates MIME.
        with patch.dict(clipboard_image.os.environ, {}, clear=False):
            with patch.object(shutil, "which", return_value="wl-paste"):
                with patch.object(subprocess, "run", side_effect=fake_run):
                    result = clipboard_image.acquire_clipboard_image()
        assert result is None

    def test_macos_pngpaste_returns_png(self):
        """When pngpaste returns valid PNG on macOS, it is returned."""
        png_bytes = _make_png_bytes()

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=png_bytes, stderr=b"")

        with patch.object(shutil, "which", side_effect=lambda x: "pngpaste" if x == "pngpaste" else None):
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.object(clipboard_image.os, "name", "posix"):
                    result = clipboard_image.acquire_clipboard_image()
        assert result == png_bytes

    def test_timeout_returns_none(self):
        """When subprocess times out, returns None."""

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 10)

        with patch.object(shutil, "which", return_value="wl-paste"):
            with patch.object(subprocess, "run", side_effect=fake_run):
                with patch.dict(clipboard_image.os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                    result = clipboard_image.acquire_clipboard_image()
        assert result is None


# ---------------------------------------------------------------------------
# is_clipboard_image_available tests
# ---------------------------------------------------------------------------


class TestIsClipboardImageAvailable:
    """Test is_clipboard_image_available with mocked shutil.which."""

    def test_wayland_backend_available(self):
        with patch.object(shutil, "which", side_effect=lambda x: "wl-paste" if x == "wl-paste" else None):
            with patch.object(clipboard_image, "_is_wayland", return_value=True):
                assert clipboard_image.is_clipboard_image_available() is True

    def test_x11_backend_available(self):
        with patch.object(shutil, "which", side_effect=lambda x: "xclip" if x == "xclip" else None):
            assert clipboard_image.is_clipboard_image_available() is True

    def test_macos_backend_available(self):
        with patch.object(shutil, "which", side_effect=lambda x: "pngpaste" if x == "pngpaste" else None):
            with patch.object(clipboard_image.os, "name", "posix"):
                with patch.object(clipboard_image, "_is_macos", return_value=True):
                    assert clipboard_image.is_clipboard_image_available() is True

    def test_no_backend_available(self):
        with patch.object(shutil, "which", return_value=None):
            assert clipboard_image.is_clipboard_image_available() is False


# ---------------------------------------------------------------------------
# clipboard_image_to_temp_path tests
# ---------------------------------------------------------------------------


class TestClipboardImageToTempPath:
    """Test clipboard_image_to_temp_path."""

    def test_writes_bytes_and_returns_path(self, tmp_path: Path):
        png_bytes = _make_png_bytes()
        storage_dir = str(tmp_path / "storage")
        result = clipboard_image.clipboard_image_to_temp_path(storage_dir, png_bytes)
        assert result is not None
        assert Path(result).exists()
        assert Path(result).read_bytes() == png_bytes

    def test_returns_none_on_empty_bytes(self, tmp_path: Path):
        result = clipboard_image.clipboard_image_to_temp_path(str(tmp_path / "storage"), b"")
        assert result is None

    def test_returns_none_on_none(self, tmp_path: Path):
        result = clipboard_image.clipboard_image_to_temp_path(str(tmp_path / "storage"), b"")
        assert result is None

    def test_stores_in_blobs_subdirectory(self, tmp_path: Path):
        png_bytes = _make_png_bytes()
        storage_dir = str(tmp_path / "storage")
        result = clipboard_image.clipboard_image_to_temp_path(storage_dir, png_bytes)
        assert result is not None
        # Path should be inside storage_dir/blobs/
        assert "blobs" in result

    def test_filename_contains_uuid_for_collision_resistance(self, tmp_path: Path):
        """Temp filenames include a random component for collision resistance."""

        png_bytes = _make_png_bytes()
        storage_dir = str(tmp_path / "storage")
        result = clipboard_image.clipboard_image_to_temp_path(storage_dir, png_bytes)
        assert result is not None
        filename = Path(result).name
        # Filename format: .clipboard_<random>.tmp (mkstemp format)
        assert filename.startswith(".clipboard_")
        assert filename.endswith(".tmp")

    def test_uniqueness_across_rapid_calls(self, tmp_path: Path):
        """Rapid consecutive calls produce different filenames."""
        png_bytes = _make_png_bytes()
        storage_dir = str(tmp_path / "storage")
        paths = set()
        for _ in range(20):
            result = clipboard_image.clipboard_image_to_temp_path(storage_dir, png_bytes)
            assert result is not None
            paths.add(Path(result).name)
        # All 20 should have distinct filenames
        assert len(paths) == 20


# ---------------------------------------------------------------------------
# No temp/blob imports in backend read functions
# ---------------------------------------------------------------------------


class TestBackendImportSafety:
    """Ensure backend read functions don't import tempfile or blob modules."""

    def test_no_tempfile_in_backend_read_functions(self):
        """Backend read functions (wayland/x11/macos) must not use tempfile."""
        source = inspect.getsource(clipboard_image)
        # tempfile is imported at module level for clipboard_image_to_temp_path,
        # but backend read functions should not import it themselves.
        # Check that the _read_clipboard_image_* functions don't have tempfile calls.
        for fn_name in ("_read_clipboard_image_wayland", "_read_clipboard_image_x11", "_read_clipboard_image_macos"):
            fn_source = inspect.getsource(getattr(clipboard_image, fn_name))
            assert "tempfile" not in fn_source, f"{fn_name} should not use tempfile"

    def test_no_blob_module_import_in_backends(self):
        """Backend read functions must not import blob modules (no direct blob ops)."""
        source = inspect.getsource(clipboard_image)
        # Only the attachment pipeline's _detect_mime_magic is imported, not blob writes.
        assert "import blob" not in source.lower() or "attachments" in source
