from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOGO_PATH = ROOT / "one" / "assets" / "logo.txt"
DOC_LOGO_LINKS = {
    ROOT / "README.md": "https://github.com/noxgle/one/blob/main/one/assets/logo.txt",
    ROOT / "docs" / "architecture.md": "https://github.com/noxgle/one/blob/main/one/assets/logo.txt",
    ROOT / "docs" / "RELEASE_CHECKLIST.md": "https://github.com/noxgle/one/blob/main/one/assets/logo.txt",
    ROOT / "docs" / "SKILLS.md": "https://github.com/noxgle/one/blob/main/one/assets/logo.txt",
    ROOT / "docs" / "EXTENSIONS.md": "https://github.com/noxgle/one/blob/main/one/assets/logo.txt",
}


def test_tui_logo_loader_uses_one_package_resource_from_any_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from one.modes import tui_mode

    expected = tuple(LOGO_PATH.read_text(encoding="utf-8").splitlines())
    monkeypatch.chdir(tmp_path)

    assert tui_mode._load_tui_logo_lines() == expected
    assert tui_mode._TUI_LOGO_LINES == expected


def test_tui_module_does_not_embed_a_second_copy_of_the_logo_artwork() -> None:
    source = (ROOT / "one" / "modes" / "tui_mode.py").read_text(encoding="utf-8")
    first_full_logo_line = LOGO_PATH.read_text(encoding="utf-8").splitlines()[0]

    assert first_full_logo_line not in source


@pytest.mark.parametrize("resource_contents", [None, "\n\t\n"])
def test_tui_logo_loader_returns_empty_when_packaged_artwork_is_missing_or_blank(
    monkeypatch: pytest.MonkeyPatch, resource_contents: str | None
) -> None:
    from one.modes import tui_mode

    class _Resource:
        def read_text(self, *, encoding: str) -> str:
            if resource_contents is None:
                raise FileNotFoundError
            return resource_contents

    class _Assets:
        def joinpath(self, _name: str) -> _Resource:
            return _Resource()

    class _Package:
        def joinpath(self, _name: str) -> _Assets:
            return _Assets()

    monkeypatch.setattr(tui_mode, "files", lambda package: _Package())

    assert tui_mode._load_tui_logo_lines() == ()


@pytest.mark.parametrize("logo_lines", [(), ("",)])
def test_tui_logo_falls_back_to_one_when_artwork_is_empty(
    monkeypatch: pytest.MonkeyPatch, logo_lines: tuple[str, ...]
) -> None:
    from one.modes import tui_mode

    class _LogoWidget:
        content = ""

        def update(self, content: str) -> None:
            self.content = content

    class _LargeLogoApp:
        size = type("Size", (), {"height": 30})()

        def __init__(self) -> None:
            self.logo_widget = _LogoWidget()

        def _main_width(self) -> int:
            return 90

        def query_one(self, *_args: object) -> _LogoWidget:
            return self.logo_widget

    monkeypatch.setattr(tui_mode, "_TUI_LOGO_LINES", logo_lines)
    app = _LargeLogoApp()

    tui_mode._OneTextualApp._refresh_logo(app)  # type: ignore[arg-type]

    assert app.logo_widget.content == "one"


def test_tui_logo_uses_full_artwork_at_small_terminal_sizes(monkeypatch) -> None:
    """Content stays full even though a physically small viewport clips it."""
    from one.modes import tui_mode

    class _LogoWidget:
        content = ""

        def update(self, content: str) -> None:
            self.content = content

    class _SmallLogoApp:
        size = type("Size", (), {"height": 1, "width": 1})()

        def __init__(self) -> None:
            self.logo_widget = _LogoWidget()

        def _main_width(self) -> int:
            return 1

        def query_one(self, *_args: object) -> _LogoWidget:
            return self.logo_widget

    logo_lines = ("full", "logo")
    monkeypatch.setattr(tui_mode, "_TUI_LOGO_LINES", logo_lines)
    app = _SmallLogoApp()

    tui_mode._OneTextualApp._refresh_logo(app)  # type: ignore[arg-type]

    assert app.logo_widget.content == "full\nlogo"


def test_main_markdown_docs_show_and_link_the_logo_asset() -> None:
    preview = "\n".join(line.rstrip() for line in LOGO_PATH.read_text(encoding="utf-8").splitlines()[:6])
    for document, link in DOC_LOGO_LINKS.items():
        content = document.read_text(encoding="utf-8")

        assert f"```\n{preview}\n```" in content
        assert f"]({link})" in content
