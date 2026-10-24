# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import gzip
import time

import httpx
import pytest

from one.tools import web_connect as web_connect_module
from one.tools.web_connect import WebConnectError, web_connect, web_connect_tool


def transport(handler):
    return httpx.MockTransport(handler)


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self):  # type: ignore[override]
        yield self.content

    async def aclose(self) -> None:
        pass


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, wait: bool = False) -> None:
        self.chunks = chunks
        self.wait = wait
        self.closed = False

    async def __aiter__(self):  # type: ignore[override]
        for chunk in self.chunks:
            yield chunk
        if self.wait:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_auto_html_extracts_best_main_content_markdown_and_absolute_links() -> None:
    html = b"""<html><head><title>Docs</title></head><body><nav>noise</nav>
    <main>tiny</main><article><h1>API</h1><p><a href='/guide'>Guide</a></p>
    <pre><code>print('ok')</code></pre><table><tr><th>A</th></tr><tr><td>B</td></tr></table></article>
    <footer>noise</footer></body></html>"""
    result = await web_connect(
        "https://example.test/start?token=secret#fragment",
        transport=transport(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=html)),
    )
    assert "# API" in result["content"]
    assert "noise" not in result["content"]
    assert "https://example.test/guide" in result["content"]
    assert "print('ok')" in result["content"]
    assert "A" in result["content"] and "B" in result["content"]
    assert result["details"]["url"] == "https://example.test/start?<redacted>"
    assert result["details"]["title"] == "Docs"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, expected", [("raw", "<h1>Title</h1>"), ("text", "Title")])
async def test_html_modes(mode: str, expected: str) -> None:
    result = await web_connect(
        "https://example.test/",
        mode,
        transport=transport(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<h1>Title</h1>")),
    )
    assert expected in result["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "included", "excluded"),
    [
        ({"preserveLinks": False}, "Guide", "https://example.test/guide"),
        ({"preserveCodeBlocks": False}, "print('ok')", "```"),
        ({"extractMainContent": False}, "Outside", ""),
    ],
)
async def test_html_extraction_options(settings: dict[str, bool], included: str, excluded: str) -> None:
    html = b"<body><p>Outside</p><article><a href='/guide'>Guide</a><pre><code>print('ok')</code></pre></article></body>"
    result = await web_connect(
        "https://example.test/",
        settings=settings,
        transport=transport(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=html)),
    )
    assert included in result["content"]
    if excluded:
        assert excluded not in result["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        ({}, b"plain text", "plain text"),
        ({"content-type": "text/plain; charset=unknown"}, "zażółć".encode(), "zażółć"),
        ({"content-type": "application/json"}, b'{"html":"<b>kept</b>"}', '{"html":"<b>kept</b>"}'),
    ],
)
async def test_textual_content_types_and_encoding(headers: dict[str, str], body: bytes, expected: str) -> None:
    result = await web_connect("https://example.test/", transport=transport(lambda request: httpx.Response(200, headers=headers, content=body)))
    assert result["content"] == expected


@pytest.mark.asyncio
async def test_json_is_preserved_in_auto_and_text_modes() -> None:
    for mode in ("auto", "text"):
        result = await web_connect(
            "https://example.test/api",
            mode,
            transport=transport(lambda request: httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"x":"<b>ok</b>"}')),
        )
        assert result["content"] == '{"x":"<b>ok</b>"}'


@pytest.mark.asyncio
async def test_redirect_is_followed_and_final_url_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final?key=secret"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"done")

    result = await web_connect("https://example.test/start", transport=transport(handler))
    assert result["content"] == "done"
    assert result["details"]["finalUrl"] == "https://example.test/final?<redacted>"


@pytest.mark.asyncio
async def test_redirects_clear_response_cookies_and_validate_every_hop() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final", "set-cookie": "session=secret"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")

    result = await web_connect("https://example.test/start", transport=transport(handler))
    assert result["content"] == "ok"
    assert len(requests) == 2
    assert "cookie" not in requests[1].headers

    with pytest.raises(WebConnectError, match="without credentials"):
        await web_connect(
            "https://example.test/",
            transport=transport(lambda request: httpx.Response(302, headers={"location": "https://@other.test/"})),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("locations, max_redirects", [(["/one"], 0), (["/one", "/two"], 1)])
async def test_redirect_limits_are_enforced(locations: list[str], max_redirects: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        location = locations[min(calls, len(locations) - 1)]
        calls += 1
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(WebConnectError, match="Redirect limit"):
        await web_connect("https://example.test/start", settings={"maxRedirects": max_redirects}, transport=transport(handler))
    assert calls == max_redirects + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 500])
async def test_non_success_statuses_are_controlled_errors(status: int) -> None:
    with pytest.raises(WebConnectError, match=f"status {status}"):
        await web_connect(
            "https://example.test/",
            transport=transport(lambda request: httpx.Response(status, headers={"content-type": "text/plain"})),
        )


@pytest.mark.asyncio
async def test_rejects_bad_runtime_args_and_urls_before_transport() -> None:
    for url in (
        "file:///tmp/a",
        "ftp://example.test/",
        "https://user:password@example.test/",
        "https://@example.test/",
        "https://example.test:99999/",
    ):
        with pytest.raises(WebConnectError):
            await web_connect(url)
    with pytest.raises(WebConnectError):
        await web_connect_tool({"url": "https://example.test", "method": "GET"})
    with pytest.raises(WebConnectError):
        await web_connect("https://example.test", "other")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://[::1]/", "http://192.168.1.10/"])
async def test_private_and_local_addresses_remain_allowed(url: str) -> None:
    result = await web_connect(
        url,
        transport=transport(lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")),
    )
    assert result["content"] == "ok"


@pytest.mark.asyncio
async def test_compressed_and_decompressed_limits_are_enforced() -> None:
    compressed = gzip.compress(b"x" * 1000)
    with pytest.raises(WebConnectError, match="decompressed"):
        await web_connect(
            "https://example.test/",
            settings={"maxResponseBytes": 100, "maxOutputChars": 20, "timeoutSec": 1, "maxRedirects": 0,
                      "extractMainContent": True, "preserveLinks": True, "preserveCodeBlocks": True},
            transport=transport(lambda request: httpx.Response(200, headers={"content-type": "text/plain", "content-encoding": "gzip"}, stream=ByteStream(compressed))),
        )
    with pytest.raises(WebConnectError, match="compressed"):
        await web_connect(
            "https://example.test/",
            settings={"maxResponseBytes": 10, "maxOutputChars": 20, "timeoutSec": 1, "maxRedirects": 0,
                      "extractMainContent": True, "preserveLinks": True, "preserveCodeBlocks": True},
            transport=transport(lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 11)),
        )


@pytest.mark.asyncio
async def test_chunked_transfer_limit_and_compressed_stream_integrity_are_enforced() -> None:
    with pytest.raises(WebConnectError, match="compressed byte limit"):
        await web_connect(
            "https://example.test/",
            settings={"maxResponseBytes": 4},
            transport=transport(lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, stream=ChunkStream([b"abc", b"de"]))),
        )
    encoded = gzip.compress(b"ok")
    for malformed in (encoded[:-2], encoded + gzip.compress(b"again")):
        with pytest.raises(WebConnectError, match="invalid compressed"):
            await web_connect(
                "https://example.test/",
                transport=transport(
                    lambda request, malformed=malformed: httpx.Response(
                        200, headers={"content-type": "text/plain", "content-encoding": "gzip"}, stream=ChunkStream([malformed])
                    )
                ),
            )


@pytest.mark.asyncio
async def test_preview_limit_retains_full_content_for_evidence_owner() -> None:
    result = await web_connect(
        "https://example.test/",
        settings={"maxOutputChars": 3, "timeoutSec": 1, "maxResponseBytes": 100, "maxRedirects": 0,
                  "extractMainContent": True, "preserveLinks": True, "preserveCodeBlocks": True},
        transport=transport(lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"abcdef")),
    )
    assert result["content"] == "abc"
    assert result["fullContent"] == "abcdef"
    assert result["details"]["truncated"] is True


@pytest.mark.asyncio
async def test_non_text_and_invalid_json_are_controlled_errors() -> None:
    with pytest.raises(WebConnectError, match="Unsupported"):
        await web_connect(
            "https://example.test/", transport=transport(lambda request: httpx.Response(200, headers={"content-type": "image/png"}, content=b"png"))
        )
    with pytest.raises(WebConnectError, match="binary"):
        await web_connect("https://example.test/", transport=transport(lambda request: httpx.Response(200, content=b"\0\x89PNG")))
    with pytest.raises(WebConnectError, match="invalid JSON"):
        await web_connect(
            "https://example.test/", transport=transport(lambda request: httpx.Response(200, headers={"content-type": "application/json"}, content=b"{"))
        )


@pytest.mark.asyncio
async def test_cancellation_closes_stream() -> None:
    stream = ChunkStream([], wait=True)
    task = asyncio.create_task(
        web_connect(
            "https://example.test/",
            transport=transport(lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, stream=stream)),
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True


@pytest.mark.asyncio
async def test_total_timeout_applies_to_synchronous_post_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_decode(body: bytes, content_type: str) -> str:
        time.sleep(0.02)
        return "done"

    monkeypatch.setattr(web_connect_module, "_decode_text", slow_decode)
    with pytest.raises(WebConnectError, match="timed out"):
        await web_connect(
            "https://example.test/",
            settings={"timeoutSec": 0.001},
            transport=transport(lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"done")),
        )


@pytest.mark.asyncio
async def test_transport_errors_are_safe_and_do_not_leak_url_credentials() -> None:
    class FailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("https://user:secret@example.test/?token=secret", request=request)

    with pytest.raises(WebConnectError, match="HTTP request failed") as exc_info:
        await web_connect("https://example.test/", transport=FailingTransport())
    assert "secret" not in str(exc_info.value)
