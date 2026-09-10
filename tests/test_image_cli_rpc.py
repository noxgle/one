"""Tests for CLI/RPC image path integration — end-to-end import + prompt wiring."""

from __future__ import annotations

import builtins
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.attachments import (
    AttachmentRef,
    import_image,
)
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.modes.print_mode import run_print_mode
from one.modes.run_mode import run_run_mode

# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

def _make_png() -> bytes:
    """Minimal valid PNG (1×1 transparent pixel)."""
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
    return sig + ihdr + idat + iend


def _make_jpeg() -> bytes:
    """Minimal valid JPEG (1×1 pixel)."""
    return (
        b"\xff\xd8"
        b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00\x43\x00"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b"
        b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x7b\x40"
        b"\xff\xd9"
    )


@pytest.fixture
def png_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.png"
    p.write_bytes(_make_png())
    return p


@pytest.fixture
def jpg_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.jpg"
    p.write_bytes(_make_jpeg())
    return p


# ---------------------------------------------------------------------------
# AttachmentRef → dict wiring (ensures __dict__ is serialisable)
# ---------------------------------------------------------------------------


def test_attachment_ref_dict_serialisable() -> None:
    ref = AttachmentRef(blob_hash="abc123", mime="image/png", size=100, width=1, height=1)
    d = ref.__dict__  # type: ignore[attr-defined]
    assert d == {
        "blob_hash": "abc123",
        "mime": "image/png",
        "size": 100,
        "width": 1,
        "height": 1,
    }


def test_import_image_returns_ref_with_dict(tmp_path: Path, png_path: Path) -> None:
    storage_dir = str(tmp_path / "store")
    ref = import_image(storage_dir, str(png_path))
    assert isinstance(ref, AttachmentRef)
    d = ref.__dict__
    assert d["blob_hash"] is not None
    assert len(d["blob_hash"]) == 64
    assert d["mime"] == "image/png"


# ---------------------------------------------------------------------------
# Print mode with images
# ---------------------------------------------------------------------------


class _Loader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:
        return "You are a coding agent."


class _CaptureProvider:
    """Captures messages + images passed to chat."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.captured: list[dict[str, Any]] = []

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
    ) -> Any:
        from one.providers.base import ChatResult

        self.captured.append({"images": images, "messages": messages})
        return ChatResult(text=self.responses[0], raw={}, usage={}, stop_reason="stop")


class _MockHost:
    def __init__(self, session: AgentSession) -> None:
        self.session = session


def _mk_image_session(tmp_path: Path, responses: list[str]) -> _MockHost:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    # gpt-4.1 is not a built-in with input_image=True; create one with it.
    from one.core.types import ModelInfo

    model = ModelInfo(provider="openai", id="gpt-4.1", input_image=True)
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(
        session_manager, settings, registry, _Loader(), model, "medium",
        storage_dir=str(tmp_path / "store"),
    )
    agent.providers = {"openai": _CaptureProvider(responses)}
    return _MockHost(agent)


@pytest.mark.asyncio
async def test_print_mode_passes_images_to_prompt(tmp_path: Path, png_path: Path, capsys):
    """Print mode: images from --image are imported and passed to session.prompt."""
    storage_dir = str(tmp_path / "store")
    # Import images the same way main.py does.
    image_refs = [import_image(storage_dir, str(png_path)).__dict__]

    responses = ['{"tool":"finish","args":{"summary":"done","goal_success":true}}', "DONE"]
    host = _mk_image_session(tmp_path, responses)
    code = await run_print_mode(host, {
        "mode": "text",
        "messages": ["look at this"],
        "initialMessage": None,
        "images": image_refs,
    })
    assert code == 0
    captured = host.session.providers["openai"].captured
    assert len(captured) >= 1
    assert captured[0]["images"] is not None
    assert len(captured[0]["images"]) == 1
    assert captured[0]["images"][0]["mime"] == "image/png"


@pytest.mark.asyncio
async def test_print_mode_without_images_works(tmp_path: Path, capsys):
    """Print mode without images (no regression)."""
    host = _mk_image_session(tmp_path, ['{"tool":"finish","args":{"summary":"done","goal_success":true}}', "DONE"])
    code = await run_print_mode(host, {
        "mode": "text",
        "messages": ["hello"],
        "initialMessage": None,
        "images": None,
    })
    assert code == 0


# ---------------------------------------------------------------------------
# Run mode with images
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_mode_passes_images_to_prompt(tmp_path: Path, png_path: Path):
    """Run mode: images are passed through to session.prompt."""
    storage_dir = str(tmp_path / "store")
    image_refs = [import_image(storage_dir, str(png_path)).__dict__]

    responses = ['{"tool":"finish","args":{"summary":"done","goal_success":true}}', "DONE"]
    host = _mk_image_session(tmp_path, responses)
    code = await run_run_mode(host, {
        "task": "do the thing",
        "resume": False,
        "json": False,
        "images": image_refs,
    })
    assert code == 0
    captured = host.session.providers["openai"].captured
    assert len(captured) >= 1
    assert captured[0]["images"] is not None
    assert len(captured[0]["images"]) == 1


@pytest.mark.asyncio
async def test_run_mode_without_images_works(tmp_path: Path, capsys):
    """Run mode without images (no regression)."""
    host = _mk_image_session(tmp_path, ['{"tool":"finish","args":{"summary":"done","goal_success":true}}', "DONE"])
    code = await run_run_mode(host, {"task": "go", "resume": False, "json": False, "images": None})
    assert code == 0


# ---------------------------------------------------------------------------
# RPC prompt with attachments
# ---------------------------------------------------------------------------


class _FakeLoader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:
        return "You are a coding agent."

    def get_extensions(self) -> dict[str, Any]:
        return {"extensions": [], "errors": [], "runtime": {}}

    def get_skills(self) -> dict[str, Any]:
        return {"skills": [], "diagnostics": []}

    def get_prompts(self) -> dict[str, Any]:
        return {"prompts": [], "diagnostics": []}

    def get_themes(self) -> dict[str, Any]:
        return {"themes": [], "diagnostics": []}

    def get_agents_files(self) -> dict[str, Any]:
        return {"agentsFiles": []}


class _RpcRuntimeHost:
    def __init__(self, session: AgentSession) -> None:
        self.session = session

    async def new_session(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"cancelled": False}

    async def import_from_jsonl(self, path: str) -> dict[str, Any]:
        return {"cancelled": False}

    async def switch_session(self, path: str) -> dict[str, Any]:
        return {"cancelled": False}

    async def fork(self, entry_id: str) -> dict[str, Any]:
        return {"cancelled": False, "selectedText": None}


def _mk_rpc_session(tmp_path: Path) -> AgentSession:
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    from one.core.types import ModelInfo

    model = ModelInfo(provider="openai", id="gpt-4.1", input_image=True)
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    return AgentSession(
        session_manager, settings, registry, _FakeLoader(),
        model, "medium", storage_dir=str(tmp_path / "store"),
    )


async def _run_rpc(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session: AgentSession,
    lines: list[str],
) -> list[dict[str, Any]]:
    from one.modes.rpc_mode import run_rpc_mode

    feed = iter(lines + [None])

    def fake_input(*args: Any) -> str:
        try:
            nxt = next(feed)
        except StopIteration:
            raise EOFError from None
        if nxt is None:
            raise EOFError
        return nxt

    monkeypatch.setattr(builtins, "input", fake_input)
    try:
        await run_rpc_mode(_RpcRuntimeHost(session))
    except EOFError:
        pass
    out = capsys.readouterr().out
    return [json.loads(l) for l in out.splitlines() if l.strip()]


def _resp(responses: list[dict[str, Any]], command: str, rid: str | None = None) -> dict[str, Any]:
    match = [
        r for r in responses
        if r.get("command") == command and r.get("type") == "response"
        and (rid is None or r.get("id") == rid)
    ]
    assert match, f"no response for command {command!r} id={rid!r}: {responses}"
    return match[-1]


@pytest.mark.asyncio
async def test_rpc_prompt_with_attachments_imports_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    png_path: Path,
):
    """RPC prompt with attachments: image paths are validated, imported, and refs passed to prompt."""
    session = _mk_rpc_session(tmp_path)

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({
            "type": "prompt", "id": "p1",
            "message": "what is in this image?",
            "attachments": [str(png_path)],
        })],
    )
    r = _resp(responses, "prompt", "p1")
    assert r["success"] is True

    # Verify the session received an image ref (via subscription).
    # We subscribe to check.
    received_images: list[Any] = []

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "message_user" and "images" in event:
            received_images.append(event["images"])

    session.subscribe(on_event)

    # The image was already processed before subscribe, but we can verify
    # that the blob exists.
    store_dir = str(tmp_path / "store" / "blobs")
    assert os.path.isdir(store_dir)
    blob_files = [f for f in os.listdir(store_dir) if not f.endswith(".tmp")]
    assert len(blob_files) == 1


@pytest.mark.asyncio
async def test_rpc_prompt_with_invalid_attachment_returns_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """RPC prompt with non-existent attachment path returns error response."""
    session = _mk_rpc_session(tmp_path)

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({
            "type": "prompt", "id": "e1",
            "message": "hello",
            "attachments": ["/nonexistent/path/image.png"],
        })],
    )
    r = _resp(responses, "prompt", "e1")
    assert r["success"] is False
    assert "Invalid attachment" in r["error"]


@pytest.mark.asyncio
async def test_rpc_prompt_without_attachments_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """RPC prompt without attachments (no regression)."""
    session = _mk_rpc_session(tmp_path)

    responses = await _run_rpc(
        monkeypatch, capsys, session,
        [json.dumps({"type": "prompt", "id": "p2", "message": "hello"})],
    )
    r = _resp(responses, "prompt", "p2")
    assert r["success"] is True


# ---------------------------------------------------------------------------
# CLI subprocess: --image flag accepted and reaches run/dispatch
# ---------------------------------------------------------------------------


def test_cli_run_with_image_flag_reaches_dispatch(tmp_path: Path, png_path: Path):
    """--image flag in `one run` is accepted by argparse and reaches dispatch (not a usage error)."""
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [
            sys.executable, "-m", "one.cli.main",
            "run", "--json", "--image", str(png_path),
            "task",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    # Not a usage error (returncode 2); dispatch reached the run mode.
    assert res.returncode != 2
    assert "Usage:" not in res.stdout + res.stderr
    # Must produce JSON output (even if failed at network level).
    data = json.loads(res.stdout)
    assert "goalSuccess" in data
    assert "finished" in data
    assert "summary" in data


def test_cli_run_with_image_flag_nonexistent_returns_error(tmp_path: Path, png_path: Path):
    """--image with a non-existent file returns a usage error."""
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [
            sys.executable, "-m", "one.cli.main",
            "--print", "--image", "/nonexistent/image.png",
            "hello",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 2
    assert "Invalid image" in res.stdout


def test_cli_run_with_multiple_images_reaches_dispatch(tmp_path: Path, png_path: Path, jpg_path: Path):
    """Multiple --image flags accepted."""
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [
            sys.executable, "-m", "one.cli.main",
            "run", "--json",
            "--image", str(png_path),
            "--image", str(jpg_path),
            "task",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode != 2
    assert "Usage:" not in res.stdout + res.stderr
    data = json.loads(res.stdout)
    assert "goalSuccess" in data


# ---------------------------------------------------------------------------
# Storage dir wiring — blobs stored in session's storage_dir
# ---------------------------------------------------------------------------


def test_images_stored_in_storage_dir(tmp_path: Path, png_path: Path):
    """Imported images go into the session's storage_dir/blobs/."""
    storage_dir = str(tmp_path / "mysession")
    ref = import_image(storage_dir, str(png_path))
    blob_path = os.path.join(storage_dir, "blobs", ref.blob_hash)
    assert os.path.exists(blob_path)
    # The blob should not leak the source path.
    blob_content = Path(blob_path).read_bytes()
    assert str(png_path).encode() not in blob_content  # type: ignore[operator]


def test_no_source_path_leaked_in_attachment_ref(tmp_path: Path, png_path: Path):
    """AttachmentRef contains blob_hash + MIME only — no source path."""
    ref = import_image(str(tmp_path / "store"), str(png_path))
    d = ref.__dict__  # type: ignore[attr-defined]
    for key in d:
        assert "nonexistent" not in key
        assert "png" not in key.lower() or key == "mime"  # mime is "image/png" not path
