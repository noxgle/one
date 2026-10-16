"""Generate the bounded packaged model-capacity snapshot from a pinned source."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# This is OpenCode's checked-in models.dev snapshot, pinned by immutable commit.
# It is deliberately not represented as a live models.dev API response.
DEFAULT_SOURCE = (
    "https://raw.githubusercontent.com/anomalyco/opencode/"
    "1ca34aeee6052f7ba0909ee9fdda6081e25e1351/packages/core/src/models-dev/snapshot.txt"
)
PROVIDERS = {
    "openai": "openai", "azure": "azure-openai", "anthropic": "anthropic",
    "google": "gemini", "openrouter": "openrouter", "ollama-cloud": "ollama-cloud",
    "xai": "xai", "deepseek": "deepseek", "mistral": "mistral", "groq": "groq",
}
MAX_SOURCE_BYTES = 10_000_000
MAX_ENTRIES = 10_000


def _read_source(source: str | None, url: str) -> bytes:
    if source:
        data = Path(source).read_bytes()
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("source exceeds byte limit")
        return data
    with httpx.stream("GET", url, timeout=60, follow_redirects=True) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise ValueError("source exceeds byte limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _limit(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 10_000_000 else None


def compact(source: dict[str, Any]) -> dict[str, dict[str, dict[str, int]]]:
    providers: dict[str, dict[str, dict[str, int]]] = {}
    count = 0
    for remote, local in PROVIDERS.items():
        raw_provider = source.get(remote)
        raw_models = raw_provider.get("models", {}) if isinstance(raw_provider, dict) else {}
        if not isinstance(raw_models, dict):
            continue
        selected: dict[str, dict[str, int]] = {}
        for model_id, model in raw_models.items():
            if not isinstance(model_id, str) or not isinstance(model, dict):
                continue
            raw_limit = model.get("limit")
            limit: dict[str, Any] = raw_limit if isinstance(raw_limit, dict) else {}
            values = {
                key: value for key, value in (
                    ("contextWindow", _limit(limit.get("context") or model.get("contextWindow"))),
                    ("inputLimit", _limit(limit.get("input") or model.get("inputLimit"))),
                    ("outputLimit", _limit(limit.get("output") or model.get("outputLimit"))),
                ) if value is not None
            }
            if values:
                selected[model_id] = values
                count += 1
                if count > MAX_ENTRIES:
                    raise ValueError("too many selected entries")
        if selected:
            providers[local] = selected
    return providers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="local pinned source file (network-free)")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE, help="public source URL recorded in output")
    parser.add_argument("--source-revision", default="1ca34aeee6052f7ba0909ee9fdda6081e25e1351")
    parser.add_argument("--output", default="one/assets/model_catalog.json")
    args = parser.parse_args()
    raw = _read_source(args.source, args.source_url)
    source = json.loads(raw)
    if not isinstance(source, dict):
        raise ValueError("source root is not an object")
    output = {
        "schemaVersion": 1,
        "sourceUrl": args.source_url,
        "sourceRevision": args.source_revision,
        "sourceSha256": hashlib.sha256(raw).hexdigest(),
        "retrievedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "providers": compact(source),
    }
    encoded = json.dumps(output, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > 4_000_000:
        raise ValueError("compact snapshot exceeds cache bound")
    Path(args.output).write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
