from __future__ import annotations

from one.resources.resource_loader import _find_closing_dash_dash_dash, _parse_frontmatter


class TestFindClosingDashDashDash:
    """Unit tests for the _find_closing_dash_dash_dash helper."""

    def test_no_closing_delimiter(self) -> None:
        assert _find_closing_dash_dash_dash("key: value") is None

    def test_only_opening_dash_no_closing(self) -> None:
        # "---body" has opening --- at pos 0 but no valid closing delimiter.
        assert _find_closing_dash_dash_dash("---body") is None

    def test_closing_on_own_line_after_content(self) -> None:
        # Position 11 = len("key: value\n") = 11
        text = "key: value\n---\nbody"
        assert _find_closing_dash_dash_dash(text) == 11

    def test_closing_not_on_own_line(self) -> None:
        # ---more is NOT a valid closing delimiter (not on its own line).
        text = "key: value\n---more"
        assert _find_closing_dash_dash_dash(text) is None

    def test_dash_in_yaml_value_followed_by_valid_closing(self) -> None:
        # A YAML value containing --- is fine as long as closing is on own line.
        text = "key: value\n---more\n---\nbody"
        # ---more at pos 11 is rejected (followed by 'm')
        # --- at pos 19 is accepted (followed by '\n')
        assert _find_closing_dash_dash_dash(text) == 19

    def test_multiple_valid_delimiters_returns_first(self) -> None:
        # First valid closing delimiter wins.
        text = "---\nkey: value\n---\n---\nbody"
        # --- at pos 0 is opening; first valid closing --- is at pos 15
        assert _find_closing_dash_dash_dash(text) == 15

    def test_closing_with_trailing_whitespace(self) -> None:
        # --- at start of line with trailing whitespace — still matches
        # because the next char after --- is space, not alphanumeric.
        text = "key: value\n---  \nbody"
        assert _find_closing_dash_dash_dash(text) == 11

    def test_closing_at_end_of_string(self) -> None:
        # --- at end of string (no char after) is valid.
        text = "key: value\n---"
        assert _find_closing_dash_dash_dash(text) == 11


class TestParseFrontmatter:
    """Integration tests for _parse_frontmatter with full-line closing rule."""

    def test_normal_frontmatter(self) -> None:
        raw = "---\nname: my-skill\ndescription: A test skill\n---\nBody text here"
        fm, body = _parse_frontmatter(raw)
        assert fm["name"] == "my-skill"
        assert fm["description"] == "A test skill"
        assert body.strip() == "Body text here"

    def test_closing_dash_dash_dash_not_on_own_line(self) -> None:
        # If closing --- is embedded in text (e.g. inside YAML value),
        # it should NOT be treated as a delimiter.
        raw = "---\nname: my---skill\ndescription: A test skill\n---more\nBody"
        fm, body = _parse_frontmatter(raw)
        # No valid closing delimiter → returns full content, empty fm.
        assert fm == {}
        assert body == raw

    def test_dash_in_yaml_value_followed_by_valid_closing(self) -> None:
        # A YAML value containing --- is fine as long as closing is on own line.
        raw = "---\nname: my-skill\npath: /foo---bar/baz\n---\nBody"
        fm, body = _parse_frontmatter(raw)
        assert fm["name"] == "my-skill"
        assert body.strip() == "Body"

    def test_no_frontmatter(self) -> None:
        raw = "Just plain text"
        fm, body = _parse_frontmatter(raw)
        assert fm == {}
        assert body == raw

    def test_starts_with_dash_but_no_closing(self) -> None:
        raw = "---some text without closing"
        fm, body = _parse_frontmatter(raw)
        assert fm == {}
        assert body == raw

    def test_empty_frontmatter_body(self) -> None:
        raw = "---\nname: test\n---"
        fm, body = _parse_frontmatter(raw)
        assert fm["name"] == "test"
        assert body == ""
