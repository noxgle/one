"""Regression checks for required Python source attribution."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import ast
import re
import tokenize
from pathlib import Path

HEADER = "# Copyright (c) 2026 picon\n# SPDX-License-Identifier: MIT\n# Source: https://github.com/noxgle/one"
CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)")
ROOT = Path(__file__).resolve().parents[1]


def _source_files() -> list[Path]:
    return sorted(
        path
        for directory in (ROOT / "one", ROOT / "tests", ROOT / "scripts")
        for path in directory.rglob("*.py")
    )


def _preamble_end(lines: list[str]) -> int:
    end = 1 if lines and lines[0].startswith("#!") else 0
    for line_number in range(min(2, len(lines))):
        if CODING_COOKIE.match(lines[line_number]):
            end = max(end, line_number + 1)
    return end


def test_python_source_attribution_headers() -> None:
    for path in _source_files():
        with tokenize.open(path) as source_file:
            source = source_file.read()
        assert source.count(HEADER) == 1, path

        lines = source.splitlines()
        header_start = lines.index(HEADER.splitlines()[0])
        module = ast.parse(source, filename=str(path))
        docstring = ast.get_docstring(module, clean=False)

        if docstring is not None:
            docstring_node = module.body[0]
            assert isinstance(docstring_node, ast.Expr)
            assert header_start == docstring_node.end_lineno, path
        else:
            assert header_start == _preamble_end(lines), path
