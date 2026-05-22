"""Integration-style tests for :class:`pydantic_httpx.Client`.

Network is replaced by ``httpx.MockTransport`` so the full client pipeline
(headers/cookies/params merging, redirects, content decoding) is still
exercised end-to-end.
"""
from __future__ import annotations

import json as stdjson
from pathlib import Path

import httpx
import pydantic
import pytest

import pydantic_httpx as ph


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class Item(pydantic.BaseModel):
    id: int
    name: str


class ApiError(pydantic.BaseModel):
    code: str
    detail: str


class Filter(pydantic.BaseModel):
    q: str
    active: bool = True
    limit: int = 10


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestClientConstruction:
    async def test_bearer_token_string(self, make_client, routes):
        captured: list[httpx.Headers] = []

        def echo(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers)
            return httpx.Response(200, json={})

        client = make_client(routes({("GET", "/ping"): echo}), bearer_token="abc")
        async with client:
            await client.get("/ping", response_class=ph.JSONResponseClass)
        assert captured[0]["authorization"] == "Bearer abc"

    async def test_bearer_token_secretstr_is_unwrapped(self, make_client, routes):
        captured: list[httpx.Headers] = []

        def echo(request):
            captured.append(request.headers)
            return httpx.Response(200, json={})

        client = make_client(
            routes({("GET", "/ping"): echo}),
            bearer_token=pydantic.SecretStr("s3cr3t"),
        )
        async with client:
            await client.get("/ping", response_class=ph.JSONResponseClass)
        assert captured[0]["authorization"] == "Bearer s3cr3t"

    async def test_default_headers_cookies_params_propagate(self, make_client, routes):
        captured: list[httpx.Request] = []

        def echo(request):
            captured.append(request)
            return httpx.Response(200, json={})

        client = make_client(
            routes({("GET", "/x"): echo}),
            headers={"X-App": "test"},
            cookies={"sid": "abc"},
            params={"v": "1"},
        )
        async with client:
            await client.get("/x", response_class=ph.JSONResponseClass)

        req = captured[0]
        assert req.headers["x-app"] == "test"
        assert req.headers["cookie"] == "sid=abc"
        assert req.url.params["v"] == "1"

    async def test_httpx_client_property_exposed(self, make_client, routes):
        client = make_client(routes({}))
        async with client:
            assert isinstance(client.httpx_client, httpx.AsyncClient)


# ---------------------------------------------------------------------------
# Request basics
# ---------------------------------------------------------------------------

class TestRequests:
    async def test_get_with_response_model(self, make_client, routes):
        handler = routes({
            ("GET", "/items/1"): lambda req: httpx.Response(200, json={"id": 1, "name": "foo"}),
        })
        client = make_client(handler)
        async with client:
            item = await client.get("/items/1", response_model=Item)
        assert item == Item(id=1, name="foo")

    async def test_get_no_response_model_returns_empty(self, make_client, routes):
        handler = routes({
            ("GET", "/x"): lambda req: httpx.Response(200, json={}),
        })
        client = make_client(handler)
        async with client:
            result = await client.get("/x")
        assert isinstance(result, ph.EmptyResponse)

    async def test_post_body_roundtrip(self, make_client, routes):
        seen: list[bytes] = []

        def echo(request):
            seen.append(request.content)
            return httpx.Response(200, json=stdjson.loads(request.content))

        client = make_client(routes({("POST", "/echo"): echo}))
        async with client:
            sent = Item(id=5, name="x")
            echoed = await client.post("/echo", body=sent, response_model=Item)

        assert echoed == sent
        assert stdjson.loads(seen[0]) == {"id": 5, "name": "x"}

    async def test_post_body_dict(self, make_client, routes):
        def echo(request):
            return httpx.Response(200, json=stdjson.loads(request.content))

        client = make_client(routes({("POST", "/echo"): echo}))
        async with client:
            echoed = await client.post(
                "/echo",
                body={"id": 9, "name": "x"},
                response_model=Item,
            )
        assert echoed == Item(id=9, name="x")

    async def test_params_pydantic_model_with_bool_lowercased(self, make_client, routes):
        captured: list[httpx.URL] = []

        def handler_fn(request):
            captured.append(request.url)
            return httpx.Response(200, json={"id": 1, "name": "x"})

        client = make_client(routes({("GET", "/items"): handler_fn}))
        async with client:
            # Explicitly set `active=True` so it ends up in the query string
            # (exclude_unset=True drops unset defaults).
            await client.get(
                "/items",
                params=Filter(q="hi", active=True),
                response_model=Item,
            )

        url = captured[0]
        assert url.params["q"] == "hi"
        assert url.params["active"] == "true"
        # `limit` defaulted and was not set → exclude_unset drops it.
        assert "limit" not in url.params

    async def test_params_unset_defaults_are_dropped(self, make_client, routes):
        captured: list[httpx.URL] = []

        def handler_fn(request):
            captured.append(request.url)
            return httpx.Response(200, json={"id": 1, "name": "x"})

        client = make_client(routes({("GET", "/items"): handler_fn}))
        async with client:
            # No defaults touched → only `q` lands on the wire.
            await client.get("/items", params=Filter(q="hi"), response_model=Item)

        url = captured[0]
        assert url.params["q"] == "hi"
        assert "active" not in url.params
        assert "limit" not in url.params

    @pytest.mark.parametrize(
        "method,client_method",
        [
            ("GET", "get"),
            ("POST", "post"),
            ("PUT", "put"),
            ("PATCH", "patch"),
            ("DELETE", "delete"),
        ],
    )
    async def test_method_shortcuts_dispatch_correctly(self, make_client, routes, method, client_method):
        seen: list[str] = []

        def record(request):
            seen.append(request.method)
            return httpx.Response(200, json={})

        client = make_client(routes({(method, "/x"): record}))
        async with client:
            await getattr(client, client_method)("/x", response_class=ph.JSONResponseClass)
        assert seen == [method]

    async def test_per_request_cookies_reach_wire(self, make_client, routes):
        captured: list[httpx.Headers] = []

        def echo(request):
            captured.append(request.headers)
            return httpx.Response(200, json={})

        client = make_client(routes({("GET", "/x"): echo}))
        async with client:
            await client.get(
                "/x",
                cookies={"session": "abc123"},
                response_class=ph.JSONResponseClass,
            )

        assert "session=abc123" in captured[0]["cookie"]

    async def test_per_request_timeout_passed(self, make_client, routes):
        # We can't easily observe the timeout from the mock transport, but
        # we can confirm the parameter is accepted without error and the
        # request still succeeds.
        client = make_client(routes({("GET", "/x"): lambda r: httpx.Response(200, json={})}))
        async with client:
            await client.get("/x", timeout=5.0, response_class=ph.JSONResponseClass)

    async def test_post_with_form_data(self, make_client, routes):
        captured: list[bytes] = []

        def echo(request):
            captured.append(request.content)
            return httpx.Response(200, json={})

        client = make_client(routes({("POST", "/form"): echo}))
        async with client:
            await client.post(
                "/form",
                data={"username": "alice", "age": 30},
                response_class=ph.JSONResponseClass,
            )

        # urlencoded form data
        body = captured[0].decode()
        assert "username=alice" in body
        assert "age=30" in body

    async def test_per_request_headers_merge_with_defaults(self, make_client, routes):
        captured: list[httpx.Headers] = []

        def echo(request):
            captured.append(request.headers)
            return httpx.Response(200, json={})

        client = make_client(
            routes({("GET", "/x"): echo}),
            headers={"X-App": "test"},
        )
        async with client:
            await client.get("/x", headers={"X-Req": "yes"}, response_class=ph.JSONResponseClass)

        h = captured[0]
        # both default and per-request headers reach the wire
        assert h["x-app"] == "test"
        assert h["x-req"] == "yes"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TestErrors:
    async def test_status_to_typed_exception(self, make_client, routes):
        handler = routes({
            ("GET", "/missing"): lambda r: httpx.Response(404, json={"detail": "nope"}),
        })
        client = make_client(handler)
        async with client:
            with pytest.raises(ph.HTTPNotFound) as exc_info:
                await client.get("/missing", response_model=Item)
        assert exc_info.value.response == {"detail": "nope"}
        assert exc_info.value.status_code == 404

    async def test_unknown_status_falls_back_to_http_error(self, make_client, routes):
        handler = routes({
            ("GET", "/teapot-ish"): lambda r: httpx.Response(499, json={"x": 1}),
        })
        client = make_client(handler)
        async with client:
            # 499 isn't a standard status — falls back to base HTTPError.
            with pytest.raises(ph.HTTPError) as exc_info:
                await client.get("/teapot-ish", response_model=Item)
        # Must not be one of the typed subclasses.
        assert type(exc_info.value) is ph.HTTPError

    async def test_error_response_model_validated(self, make_client, routes):
        handler = routes({
            ("GET", "/u"): lambda r: httpx.Response(401, json={"code": "unauth", "detail": "no"}),
        })
        client = make_client(handler)
        async with client:
            with pytest.raises(ph.HTTPUnauthorized) as exc_info:
                await client.get("/u", error_response_models={401: ApiError}, response_model=Item)
        assert isinstance(exc_info.value.response, ApiError)
        assert exc_info.value.response == ApiError(code="unauth", detail="no")

    async def test_client_default_error_models_used(self, make_client, routes):
        handler = routes({
            ("GET", "/u"): lambda r: httpx.Response(401, json={"code": "x", "detail": "y"}),
        })
        client = make_client(handler, error_response_models={401: ApiError})
        async with client:
            with pytest.raises(ph.HTTPUnauthorized) as exc_info:
                await client.get("/u", response_model=Item)
        assert isinstance(exc_info.value.response, ApiError)

    async def test_per_request_error_models_override_client_defaults(self, make_client, routes):
        class OtherError(pydantic.BaseModel):
            other: str

        handler = routes({
            ("GET", "/u"): lambda r: httpx.Response(401, json={"other": "hi"}),
        })
        client = make_client(handler, error_response_models={401: ApiError})
        async with client:
            with pytest.raises(ph.HTTPUnauthorized) as exc_info:
                await client.get(
                    "/u",
                    response_model=Item,
                    error_response_models={401: OtherError},
                )
        assert isinstance(exc_info.value.response, OtherError)

    async def test_non_json_error_body_still_raises_typed_exception(self, make_client, routes):
        # Without an explicit error_response_model, a non-JSON 5xx body must
        # not be swallowed as ResponseParseError — the user expects the
        # status-based mapping to win, with the raw text on `.response`.
        handler = routes({
            ("GET", "/broken"): lambda r: httpx.Response(
                500,
                content=b"<html>oops</html>",
                headers={"content-type": "text/html"},
            ),
        })
        client = make_client(handler)
        async with client:
            with pytest.raises(ph.HTTPInternalServerError) as exc_info:
                await client.get("/broken", response_model=Item)
        assert exc_info.value.response == "<html>oops</html>"

    async def test_empty_error_body_still_raises_typed_exception(self, make_client, routes):
        # 404 with no body at all -> HTTPNotFound(None), not ResponseParseError.
        handler = routes({
            ("GET", "/empty"): lambda r: httpx.Response(404, content=b""),
        })
        client = make_client(handler)
        async with client:
            with pytest.raises(ph.HTTPNotFound) as exc_info:
                await client.get("/empty", response_model=Item)
        assert exc_info.value.response is None

    async def test_response_parse_error_when_error_model_does_not_match(self, make_client, routes):
        handler = routes({
            ("GET", "/u"): lambda r: httpx.Response(401, json={"wrong": "schema"}),
        })
        client = make_client(handler)
        async with client:
            with pytest.raises(ph.ResponseParseError):
                # ApiError requires `code` + `detail` — payload doesn't match.
                await client.get("/u", error_response_models={401: ApiError}, response_model=Item)


# ---------------------------------------------------------------------------
# File transfer
# ---------------------------------------------------------------------------

class TestFiles:
    async def test_download_file_writes_streamed_chunks(self, make_client, routes, tmp_path: Path):
        body = b"A" * 10_000 + b"B" * 10_000

        handler = routes({
            ("GET", "/blob"): lambda r: httpx.Response(200, content=body),
        })
        client = make_client(handler)
        target = tmp_path / "out.bin"
        async with client:
            result = await client.download_file("/blob", target, chunk_size=4096)
        assert result == target
        assert target.read_bytes() == body

    async def test_download_file_propagates_errors(self, make_client, routes, tmp_path: Path):
        handler = routes({
            ("GET", "/blob"): lambda r: httpx.Response(404, json={"detail": "gone"}),
        })
        client = make_client(handler)
        target = tmp_path / "out.bin"
        async with client:
            with pytest.raises(ph.HTTPNotFound):
                await client.download_file("/blob", target)
        # No file should be written on error.
        assert not target.exists()

    async def test_upload_file_sends_multipart(self, make_client, routes, tmp_path: Path):
        captured: list[bytes] = []

        def receive(request):
            captured.append(request.content)
            return httpx.Response(200, json={})

        f = tmp_path / "data.bin"
        f.write_bytes(b"payload-bytes")

        client = make_client(routes({("POST", "/upload"): receive}))
        async with client:
            await client.upload_file("/upload", f, response_class=ph.JSONResponseClass)

        body = captured[0]
        assert b"payload-bytes" in body
        # multipart form delimiter is present
        assert b"Content-Disposition" in body
        # Original file name must be preserved in Content-Disposition so APIs
        # that key on the upload's filename still work.
        assert b'filename="data.bin"' in body

    async def test_stream_file_sends_chunked_content(self, make_client, routes, tmp_path: Path):
        captured: list[bytes] = []

        def receive(request):
            captured.append(request.content)
            return httpx.Response(200, json={})

        payload = b"X" * 200_000
        f = tmp_path / "big.bin"
        f.write_bytes(payload)

        client = make_client(routes({("POST", "/upload"): receive}))
        async with client:
            await client.stream_file("/upload", f, response_class=ph.JSONResponseClass)

        assert captured[0] == payload


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    async def test_context_manager_closes_client(self, make_client, routes):
        client = make_client(routes({}))
        async with client:
            assert not client.httpx_client.is_closed
        assert client.httpx_client.is_closed

    async def test_explicit_close(self, make_client, routes):
        client = make_client(routes({}))
        await client.close()
        assert client.httpx_client.is_closed

    async def test_request_raises_when_no_response_class(self, make_client, routes):
        client = make_client(routes({}))
        # Force-clear both client default and per-request value to hit the guard.
        client._response_class = None  # type: ignore[assignment]
        async with client:
            with pytest.raises(ValueError, match="response_class"):
                await client.request("GET", "/x")
