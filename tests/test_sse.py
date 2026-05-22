"""Server-Sent Events edge cases for :meth:`pydantic_httpx.Client.sse`."""
from __future__ import annotations

import httpx
import pydantic
import pytest

import pydantic_httpx as ph
from pydantic_httpx.responses import JSONSSEEventClass
from pydantic_httpx.responses import PlainTextSSEEventClass
from pydantic_httpx.responses import RawSSEEventClass


class Chunk(pydantic.BaseModel):
    delta: str
    index: int


class ApiError(pydantic.BaseModel):
    code: str
    detail: str


def _sse_response(frames: list[str], status: int = 200) -> httpx.Response:
    body = "".join(frames).encode()
    return httpx.Response(
        status,
        headers={"content-type": "text/event-stream"},
        content=body,
    )


THREE_FRAMES = [
    'event: message\ndata: {"delta": "hello", "index": 0}\n\n',
    'event: message\ndata: {"delta": " world", "index": 1}\n\n',
    'event: done\ndata: {"delta": "", "index": 2}\n\n',
]


class TestSSEBasics:
    async def test_default_yields_sse_event_pydantic_models(self, make_client, routes):
        handler = routes({("GET", "/stream"): lambda r: _sse_response(THREE_FRAMES)})
        client = make_client(handler)

        async with client:
            events = [ev async for ev in client.sse("/stream")]

        assert all(isinstance(e, ph.SSEEvent) for e in events)
        assert [e.event for e in events] == ["message", "message", "done"]
        assert events[0].data == '{"delta": "hello", "index": 0}'

    async def test_with_response_model_yields_parsed_models(self, make_client, routes):
        handler = routes({("GET", "/stream"): lambda r: _sse_response(THREE_FRAMES)})
        client = make_client(handler)

        async with client:
            chunks = [c async for c in client.sse("/stream", response_model=Chunk)]

        assert chunks == [
            Chunk(delta="hello", index=0),
            Chunk(delta=" world", index=1),
            Chunk(delta="", index=2),
        ]

    async def test_raw_event_class_yields_httpx_sse_objects(self, make_client, routes):
        handler = routes({("GET", "/stream"): lambda r: _sse_response(THREE_FRAMES)})
        client = make_client(handler)

        async with client:
            events = [
                ev async for ev in client.sse("/stream", event_class=RawSSEEventClass)
            ]

        # Opt-in: third-party type leaks through ONLY when explicitly requested.
        assert all(isinstance(e, ph.ServerSentEvent) for e in events)

    async def test_json_event_class_returns_dicts(self, make_client, routes):
        handler = routes({("GET", "/stream"): lambda r: _sse_response(THREE_FRAMES)})
        client = make_client(handler)

        async with client:
            values = [v async for v in client.sse("/stream", event_class=JSONSSEEventClass)]

        assert values == [
            {"delta": "hello", "index": 0},
            {"delta": " world", "index": 1},
            {"delta": "", "index": 2},
        ]

    async def test_plain_text_event_class_returns_data_strings(self, make_client, routes):
        handler = routes({("GET", "/stream"): lambda r: _sse_response(THREE_FRAMES)})
        client = make_client(handler)

        async with client:
            values = [v async for v in client.sse("/stream", event_class=PlainTextSSEEventClass)]

        assert values[0] == '{"delta": "hello", "index": 0}'


class TestSSERequest:
    async def test_method_post_with_body(self, make_client, routes):
        seen: list[tuple[str, bytes]] = []

        def handler_fn(request):
            seen.append((request.method, request.content))
            return _sse_response(THREE_FRAMES)

        client = make_client(routes({("POST", "/stream"): handler_fn}))
        async with client:
            async for _ in client.sse(
                "/stream", method="POST", body={"topic": "weather"}, response_model=Chunk,
            ):
                pass

        method, body = seen[0]
        assert method == "POST"
        assert body == b'{"topic":"weather"}'

    async def test_params_and_headers_passed(self, make_client, routes):
        captured: list[httpx.Request] = []

        def handler_fn(request):
            captured.append(request)
            return _sse_response(THREE_FRAMES)

        client = make_client(routes({("GET", "/stream"): handler_fn}))
        async with client:
            async for _ in client.sse(
                "/stream",
                params={"channel": "alpha"},
                headers={"X-Trace": "abc"},
            ):
                pass

        req = captured[0]
        assert req.url.params["channel"] == "alpha"
        assert req.headers["x-trace"] == "abc"


class TestSSEEdgeCases:
    async def test_empty_stream_yields_nothing(self, make_client, routes):
        handler = routes({("GET", "/empty"): lambda r: _sse_response([])})
        client = make_client(handler)

        async with client:
            events = [ev async for ev in client.sse("/empty")]
        assert events == []

    async def test_event_without_data_field(self, make_client, routes):
        # Lone ":heartbeat" comments + event without data.
        handler = routes({
            ("GET", "/hb"): lambda r: _sse_response([
                ":heartbeat\n\n",
                "event: ping\n\n",
            ]),
        })
        client = make_client(handler)

        async with client:
            events = [ev async for ev in client.sse("/hb")]

        # heartbeat comment is dropped by the SSE parser; only the "ping" event surfaces.
        assert len(events) == 1
        assert events[0].event == "ping"
        assert events[0].data == ""

    async def test_multiline_data_field_is_joined(self, make_client, routes):
        handler = routes({
            ("GET", "/multi"): lambda r: _sse_response([
                'event: message\ndata: line-1\ndata: line-2\n\n',
            ]),
        })
        client = make_client(handler)

        async with client:
            events = [ev async for ev in client.sse("/multi")]

        # Per SSE spec, multiple `data:` fields are joined with newlines.
        assert events[0].data == "line-1\nline-2"

    async def test_error_status_raises_typed_exception(self, make_client, routes):
        handler = routes({
            ("GET", "/forbidden"): lambda r: httpx.Response(
                403, json={"code": "nope", "detail": "denied"},
            ),
        })
        client = make_client(handler)

        async with client:
            with pytest.raises(ph.HTTPForbidden) as exc_info:
                async for _ in client.sse("/forbidden", error_response_models={403: ApiError}):
                    pass
        assert isinstance(exc_info.value.response, ApiError)
        assert exc_info.value.response.detail == "denied"

    async def test_invalid_event_json_with_response_model_raises(self, make_client, routes):
        handler = routes({
            ("GET", "/bad"): lambda r: _sse_response(['event: message\ndata: not-json\n\n']),
        })
        client = make_client(handler)

        async with client:
            with pytest.raises(pydantic.ValidationError):
                async for _ in client.sse("/bad", response_model=Chunk):
                    pass
