"""Bounded GET-only HTTP retrieval used by the ``web_connect`` native tool.

This module deliberately has no session or evidence dependency.  The caller can
persist ``fullContent`` before presenting the ``content`` preview.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
import math
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify

_MODES = frozenset({"auto", "raw", "text"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_JSON_TYPES = ("application/json", "+json")
_REMOVABLE_SELECTORS = (
    "script, style, nav, footer, aside, iframe, noscript, "
    "[role='navigation'], [role='banner'], [role='contentinfo'], "
    ".advertisement, .ads, .ad, .cookie, .cookie-banner, .consent"
)
_MAIN_SELECTORS = ("article", "main", "[role='main']", ".post-content", ".entry-content", ".markdown-body")


class WebConnectError(ValueError):
    """A controlled error safe to expose as a tool failure."""


@dataclass(frozen=True)
class WebConnectSettings:
    timeout_sec: float = 15
    max_response_bytes: int = 2_000_000
    max_output_chars: int = 12_000
    max_redirects: int = 5
    extract_main_content: bool = True
    preserve_links: bool = True
    preserve_code_blocks: bool = True

    @classmethod
    def from_mapping(cls, settings: Mapping[str, Any] | None) -> WebConnectSettings:
        source = settings or {}
        if not isinstance(source, Mapping):
            raise WebConnectError("webConnect settings must be an object")
        values = {
            "timeout_sec": source.get("timeoutSec", 15),
            "max_response_bytes": source.get("maxResponseBytes", 2_000_000),
            "max_output_chars": source.get("maxOutputChars", 12_000),
            "max_redirects": source.get("maxRedirects", 5),
            "extract_main_content": source.get("extractMainContent", True),
            "preserve_links": source.get("preserveLinks", True),
            "preserve_code_blocks": source.get("preserveCodeBlocks", True),
        }
        try:
            return cls(**values)
        except TypeError as exc:
            raise WebConnectError("Invalid webConnect settings") from exc


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port is not None else "")
    query = "<redacted>" if parts.query else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _validate_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebConnectError("url must be a non-empty HTTP(S) URL")
    try:
        parts = urlsplit(value.strip())
        port = parts.port  # Raises for an invalid port.
    except ValueError as exc:
        raise WebConnectError("url has an invalid port") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or "@" in parts.netloc:
        raise WebConnectError("url must be an HTTP(S) URL with host and without credentials")
    # A fragment is client-side only and must never be transmitted.
    netloc = parts.hostname
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def _validate_settings(settings: WebConnectSettings) -> None:
    numeric = (settings.timeout_sec, settings.max_response_bytes, settings.max_output_chars)
    if any(isinstance(value, bool) for value in numeric):
        raise WebConnectError("webConnect numeric settings must not be booleans")
    if not isinstance(settings.timeout_sec, (int, float)) or not math.isfinite(settings.timeout_sec) or settings.timeout_sec <= 0:
        raise WebConnectError("webConnect.timeoutSec must be a positive finite number")
    if not isinstance(settings.max_response_bytes, int) or settings.max_response_bytes <= 0:
        raise WebConnectError("webConnect.maxResponseBytes must be a positive integer")
    if not isinstance(settings.max_output_chars, int) or settings.max_output_chars <= 0:
        raise WebConnectError("webConnect.maxOutputChars must be a positive integer")
    if isinstance(settings.max_redirects, bool) or not isinstance(settings.max_redirects, int) or settings.max_redirects < 0:
        raise WebConnectError("webConnect.maxRedirects must be a non-negative integer")
    if any(not isinstance(value, bool) for value in (settings.extract_main_content, settings.preserve_links, settings.preserve_code_blocks)):
        raise WebConnectError("webConnect extraction settings must be booleans")


def _content_kind(content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if any(media_type == html_type for html_type in _HTML_TYPES):
        return "html"
    if media_type == "application/json" or media_type.endswith("+json"):
        return "json"
    if media_type.startswith("text/") or media_type in {"application/xml", "application/javascript"}:
        return "text"
    if not media_type:
        return "text"
    raise WebConnectError(f"Unsupported response Content-Type: {media_type}")


def _looks_binary(body: bytes) -> bool:
    """Conservatively reject untyped binary payloads rather than emitting mojibake."""
    if not body:
        return False
    if b"\0" in body:
        return True
    controls = sum(byte < 32 and byte not in {9, 10, 12, 13} for byte in body)
    return controls / len(body) > 0.1


def _charset(content_type: str) -> str:
    match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _decode_text(body: bytes, content_type: str) -> str:
    try:
        return body.decode(_charset(content_type), errors="strict")
    except (LookupError, UnicodeDecodeError):
        # A response with no usable charset is still a textual response; make the
        # replacement visible rather than guessing a locale-specific encoding.
        return body.decode("utf-8", errors="replace")


def _absolute_links(soup: BeautifulSoup, base_url: str) -> None:
    for tag in soup.find_all(["a", "img", "link"]):
        attribute = "href" if tag.name in {"a", "link"} else "src"
        value = tag.get(attribute)
        if not isinstance(value, str):
            continue
        absolute = urljoin(base_url, value)
        if urlsplit(absolute).scheme in {"http", "https", "mailto"}:
            tag[attribute] = absolute
        else:
            tag.attrs.pop(attribute, None)


def _html_content(html: str, final_url: str, mode: str, settings: WebConnectSettings) -> tuple[str, str | None]:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    if mode == "raw":
        return html, title
    for tag in soup.select(_REMOVABLE_SELECTORS):
        tag.decompose()
    if settings.extract_main_content:
        candidates = soup.select(", ".join(_MAIN_SELECTORS))
        if candidates:
            root = max(candidates, key=lambda tag: len(tag.get_text(" ", strip=True)))
        else:
            root = soup.body or soup
    else:
        root = soup.body or soup
    if mode == "text":
        return root.get_text("\n", strip=True), title
    _absolute_links(root, final_url)
    if not settings.preserve_links:
        for tag in root.find_all("a"):
            tag.unwrap()
    if not settings.preserve_code_blocks:
        for tag in root.find_all(["pre", "code"]):
            tag.unwrap()
    content = markdownify(str(root), heading_style="ATX", bullets="-")
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    return content, title


def _decompress(data: bytes, encoding: str, limit: int) -> bytes:
    encoding = encoding.lower().strip()
    if encoding in {"", "identity"}:
        output = data
    elif encoding in {"gzip", "deflate"}:
        try:
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
            output = decompressor.decompress(data, limit + 1)
            if len(output) > limit:
                raise WebConnectError("Response exceeds the decompressed byte limit")
            if len(output) <= limit:
                output += decompressor.flush(limit + 1 - len(output))
            if len(output) > limit:
                raise WebConnectError("Response exceeds the decompressed byte limit")
            # zlib otherwise accepts incomplete gzip streams and ignores data
            # after a completed member.  A response has exactly one encoding.
            if not decompressor.eof or decompressor.unused_data:
                raise WebConnectError("Response has invalid compressed content")
        except zlib.error as exc:
            raise WebConnectError("Response has invalid compressed content") from exc
    else:
        raise WebConnectError(f"Unsupported Content-Encoding: {encoding}")
    if len(output) > limit:
        raise WebConnectError("Response exceeds the decompressed byte limit")
    return output


async def _fetch(url: str, settings: WebConnectSettings, transport: httpx.AsyncBaseTransport | None) -> tuple[bytes, httpx.Response, str]:
    current = url
    redirects = 0
    timeout = httpx.Timeout(settings.timeout_sec)
    async with httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False) as client:
        while True:
            # This client is deliberately stateless across redirect hops.  In
            # particular, a Set-Cookie response must not affect the next URL.
            client.cookies.clear()
            request = client.build_request("GET", current, headers={"Accept-Encoding": "gzip, deflate, identity"})
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise WebConnectError("HTTP request failed") from exc
            try:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location")
                    if not location:
                        raise WebConnectError("Redirect response has no Location header")
                    if redirects >= settings.max_redirects:
                        raise WebConnectError("Redirect limit exceeded")
                    current = _validate_url(urljoin(current, location))
                    redirects += 1
                    continue
                if not 200 <= response.status_code < 300:
                    raise WebConnectError(f"HTTP request returned status {response.status_code}")
                chunks: list[bytes] = []
                compressed_size = 0
                if response.is_stream_consumed:
                    chunks = [response.content]
                    compressed_size = len(response.content)
                    if compressed_size > settings.max_response_bytes:
                        raise WebConnectError("Response exceeds the compressed byte limit")
                else:
                    async for chunk in response.aiter_raw():
                        compressed_size += len(chunk)
                        if compressed_size > settings.max_response_bytes:
                            raise WebConnectError("Response exceeds the compressed byte limit")
                        chunks.append(chunk)
                body = _decompress(b"".join(chunks), response.headers.get("content-encoding", ""), settings.max_response_bytes)
                return body, response, current
            finally:
                try:
                    await response.aclose()
                except httpx.HTTPError as exc:
                    raise WebConnectError("HTTP request failed") from exc


async def web_connect(
    url: str,
    mode: str = "auto",
    *,
    settings: Mapping[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Fetch one URL and return bounded presentation content plus full content.

    ``transport`` is only an injection seam for tests. Callers must persist
    ``fullContent`` before discarding it when durable evidence is enabled.
    """
    if not isinstance(mode, str) or mode not in _MODES:
        raise WebConnectError("mode must be one of: auto, raw, text")
    requested_url = _validate_url(url)
    parsed_settings = WebConnectSettings.from_mapping(settings)
    _validate_settings(parsed_settings)
    deadline = asyncio.get_running_loop().time() + parsed_settings.timeout_sec
    try:
        async with asyncio.timeout_at(deadline):
            body, response, final_url = await _fetch(requested_url, parsed_settings, transport)
            content_type = response.headers.get("content-type", "")
            kind = _content_kind(content_type)
            if not content_type and _looks_binary(body):
                raise WebConnectError("Unsupported binary response without Content-Type")
            text = _decode_text(body, content_type)
            title: str | None = None
            if kind == "html":
                content, title = await asyncio.to_thread(_html_content, text, final_url, mode, parsed_settings)
            elif kind == "json":
                if mode == "raw":
                    content = text
                else:
                    try:
                        json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise WebConnectError("Response declared JSON but contains invalid JSON") from exc
                    content = text
            else:
                content = text
            # Decoding and JSON validation execute synchronously.  Check the
            # shared deadline after them rather than letting a CPU-bound step
            # make a successful result outlive the total operation timeout.
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
    except TimeoutError as exc:
        raise WebConnectError("web_connect timed out") from exc
    except httpx.HTTPError as exc:
        # httpx exception messages can contain the requested URL (including
        # credentials/query strings), so do not expose them to tool callers.
        raise WebConnectError("HTTP request failed") from exc

    truncated = len(content) > parsed_settings.max_output_chars
    preview = content[: parsed_settings.max_output_chars] if truncated else content
    details = {
        "url": _redact_url(requested_url),
        "finalUrl": _redact_url(final_url),
        "statusCode": response.status_code,
        "contentType": content_type.split(";", 1)[0].strip().lower() or None,
        "mode": mode,
        "title": title,
        "truncated": truncated,
        "responseBytes": len(body),
    }
    return {"content": preview, "fullContent": content, "details": details}


async def web_connect_tool(
    args: Mapping[str, Any], *, settings: Mapping[str, Any] | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Strict runtime-argument adapter for dispatcher integration."""
    if not isinstance(args, Mapping) or set(args) - {"url", "mode"} or "url" not in args:
        raise WebConnectError("web_connect accepts exactly url and optional mode")
    return await web_connect(args["url"], args.get("mode", "auto"), settings=settings, transport=transport)
