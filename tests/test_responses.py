"""Unit tests for :mod:`pydantic_httpx.responses`."""
from __future__ import annotations

from pathlib import Path

import httpx
import pydantic
import pytest
from httpx_sse import ServerSentEvent

from pydantic_httpx.responses import JSONResponseClass
from pydantic_httpx.responses import JSONSSEEventClass
from pydantic_httpx.responses import NoneResponseClass
from pydantic_httpx.responses import PlainTextResponseClass
from pydantic_httpx.responses import PlainTextSSEEventClass
from pydantic_httpx.responses import PydanticModelResponseClass
from pydantic_httpx.responses import RawResponseClass
from pydantic_httpx.responses import RawSSEEventClass
from pydantic_httpx.responses import ResponseClass
from pydantic_httpx.responses import SSEEvent
from pydantic_httpx.responses import SSEEventClass
from pydantic_httpx.responses import SSEPydanticEventClass
from pydantic_httpx.responses import StreamResponseClass
from pydantic_httpx.types import EmptyResponse


class _Item(pydantic.BaseModel):
    id: int
    name: str


def _make_response(content: bytes = b"", status: int = 200, headers: dict | None = None) -> httpx.Response:
    """Build a fully-formed httpx.Response with a backing request."""
    request = httpx.Request("GET", "http://api.test/x")
    response = httpx.Response(status, content=content, headers=headers, request=request)
    return response


# ---------------------------------------------------------------------------
# Response classes
# ---------------------------------------------------------------------------

class TestResponseClasses:
    def test_response_class_is_abstract(self):
        with pytest.raises(TypeError):
            ResponseClass(_make_response())  # type: ignore[abstract]

    async def test_raw_returns_response(self):
        response = _make_response(b"hello")
        assert await RawResponseClass(response).parse() is response

    async def test_none_returns_none(self):
        assert await NoneResponseClass(_make_response()).parse() is None

    async def test_plain_text_uses_charset(self):
        response = _make_response("привет".encode("utf-8"))
        assert await PlainTextResponseClass(response).parse() == "привет"

    async def test_plain_text_custom_charset(self):
        response = _make_response("café".encode("latin-1"))

        class LatinText(PlainTextResponseClass):
            charset = "latin-1"

        assert await LatinText(response).parse() == "café"

    async def test_json_response(self):
        response = _make_response(b'{"a": 1, "b": [2, 3]}', headers={"content-type": "application/json"})
        assert await JSONResponseClass(response).parse() == {"a": 1, "b": [2, 3]}

    async def test_pydantic_model_with_model(self):
        response = _make_response(b'{"id": 7, "name": "x"}')
        item = await PydanticModelResponseClass(response).parse(response_model=_Item)
        assert item == _Item(id=7, name="x")

    async def test_pydantic_model_without_model_returns_empty(self):
        response = _make_response(b'{}')
        result = await PydanticModelResponseClass(response).parse()
        assert isinstance(result, EmptyResponse)

    async def test_stream_writes_to_file(self, tmp_path: Path):
        # Build a streaming response by feeding chunks through an async generator.
        async def chunks():
            yield b"chunk-1-"
            yield b"chunk-2-"
            yield b"chunk-3"

        request = httpx.Request("GET", "http://api.test/file")
        response = httpx.Response(200, content=chunks(), request=request)

        target = tmp_path / "out.bin"
        result = await StreamResponseClass(response).parse(filepath=target, chunk_size=4)

        assert result == target
        assert target.read_bytes() == b"chunk-1-chunk-2-chunk-3"

    def test_stream_marked_as_streamed(self):
        # The Client uses this attribute to choose ``client.stream(...)`` over
        # the non-streaming code path.
        assert StreamResponseClass.streamed is True
        assert PydanticModelResponseClass.streamed is False


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

class TestSSEEvent:
    def test_defaults(self):
        e = SSEEvent()
        assert e.event == "message"
        assert e.data == ""
        assert e.id == ""
        assert e.retry is None

    def test_construct_from_fields(self):
        e = SSEEvent(event="done", data="bye", id="42", retry=100)
        assert (e.event, e.data, e.id, e.retry) == ("done", "bye", "42", 100)


class TestSSEEventClasses:
    @pytest.fixture
    def event(self) -> ServerSentEvent:
        return ServerSentEvent(
            event="message",
            data='{"delta": "hello", "index": 0}',
            id="abc",
            retry=500,
        )

    def test_base_is_abstract(self, event):
        with pytest.raises(TypeError):
            SSEEventClass(event)  # type: ignore[abstract]

    async def test_raw_returns_underlying(self, event):
        out = await RawSSEEventClass(event).parse()
        assert out is event

    async def test_pydantic_without_model_returns_sse_event(self, event):
        out = await SSEPydanticEventClass(event).parse()
        assert isinstance(out, SSEEvent)
        assert out.event == "message"
        assert out.data == '{"delta": "hello", "index": 0}'
        assert out.id == "abc"
        assert out.retry == 500

    async def test_pydantic_with_model_validates_json(self, event):
        class Chunk(pydantic.BaseModel):
            delta: str
            index: int

        out = await SSEPydanticEventClass(event).parse(response_model=Chunk)
        assert out == Chunk(delta="hello", index=0)

    async def test_pydantic_with_model_validates_invalid_json_raises(self, event):
        bad = ServerSentEvent(event="message", data="not-json")

        class Chunk(pydantic.BaseModel):
            delta: str

        with pytest.raises(pydantic.ValidationError):
            await SSEPydanticEventClass(bad).parse(response_model=Chunk)

    async def test_json_event_class_returns_raw_dict(self, event):
        assert await JSONSSEEventClass(event).parse() == {"delta": "hello", "index": 0}

    async def test_plain_text_event_class_returns_data(self, event):
        assert await PlainTextSSEEventClass(event).parse() == '{"delta": "hello", "index": 0}'
