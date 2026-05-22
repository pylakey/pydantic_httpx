import abc
import logging
from os import PathLike
from typing import Any
from typing import Generic
from typing import TypeVar
from typing import cast

import aiofiles
import httpx
import pydantic
from httpx_sse import ServerSentEvent

from .types import EmptyResponse
from .utils import DEFAULT_DOWNLOAD_CHUNK_SIZE

ResponseContentType = TypeVar('ResponseContentType')
SSEPayloadType = TypeVar('SSEPayloadType')

PathLikeT = str | PathLike[str]


class ResponseClass(abc.ABC, Generic[ResponseContentType]):
    """Base class for response parsers.

    Subclasses set ``streamed = True`` if they need the underlying response to
    stay open as a streaming response (``client.stream(...)``) instead of being
    read into memory by ``client.request(...)``.
    """

    charset: str = "utf-8"
    streamed: bool = False

    def __init__(self, response: httpx.Response):
        self.response = response
        self.logger = logging.getLogger(self.__class__.__name__)

    @abc.abstractmethod
    async def parse(self, *args: Any, **kwargs: Any) -> ResponseContentType | None:
        ...


class RawResponseClass(ResponseClass[httpx.Response]):
    async def parse(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return self.response


class NoneResponseClass(ResponseClass[None]):
    async def parse(self, *args: Any, **kwargs: Any) -> None:
        return None


class PlainTextResponseClass(ResponseClass[str]):
    async def parse(self, *args: Any, **kwargs: Any) -> str:
        return self.response.content.decode(self.charset)


_JsonBaseFields = str | int | float | bool | None
Json = _JsonBaseFields | dict[str, Any] | list[Any]


class JSONResponseClass(ResponseClass[Json]):
    async def parse(self, *args: Any, **kwargs: Any) -> Json | None:
        return self.response.json()


PydanticModel = TypeVar('PydanticModel')


class PydanticModelResponseClass(ResponseClass[PydanticModel]):
    async def parse(
            self,
            *args: Any,
            response_model: type[PydanticModel] | None = None,
            **kwargs: Any,
    ) -> PydanticModel:
        if response_model is None:
            return cast(PydanticModel, EmptyResponse())

        # validate_json parses + validates in one pass (faster than json() + validate_python)
        return pydantic.TypeAdapter(response_model).validate_json(self.response.content)


class StreamResponseClass(ResponseClass[PathLikeT]):
    streamed: bool = True

    async def parse(
            self,
            *args: Any,
            filepath: PathLikeT,
            chunk_size: int = DEFAULT_DOWNLOAD_CHUNK_SIZE,
            **kwargs: Any,
    ) -> PathLikeT:
        async with aiofiles.open(filepath, 'wb') as fd:
            async for chunk in self.response.aiter_bytes(chunk_size):
                await fd.write(chunk)
        return filepath


# ---------------------------------------------------------------------------
# Server-Sent Events
# ---------------------------------------------------------------------------

class SSEEvent(pydantic.BaseModel):
    """Pydantic-friendly mirror of :class:`httpx_sse.ServerSentEvent`.

    Returned by :class:`SSEPydanticEventClass` (the default ``event_class``)
    when no ``response_model`` is supplied to ``Client.sse(...)``.
    """

    event: str = "message"
    data: str = ""
    id: str = ""
    retry: int | None = None


class SSEEventClass(abc.ABC, Generic[SSEPayloadType]):
    """Base class for parsing a single Server-Sent Event payload."""

    def __init__(self, event: ServerSentEvent):
        self.event = event

    @abc.abstractmethod
    async def parse(self, *args: Any, **kwargs: Any) -> SSEPayloadType:
        ...


class RawSSEEventClass(SSEEventClass[ServerSentEvent]):
    """Yields the underlying :class:`httpx_sse.ServerSentEvent` unchanged."""

    async def parse(self, *args: Any, **kwargs: Any) -> ServerSentEvent:
        return self.event


class SSEPydanticEventClass(SSEEventClass[Any]):
    """Default SSE parser.

    With ``response_model`` set: parses ``event.data`` as JSON and validates
    against the model (any type supported by ``pydantic.TypeAdapter`` —
    BaseModel, dataclass, ``list[Model]``, etc.). Without it: returns an
    :class:`SSEEvent` pydantic model containing the raw fields — never leaks
    the third-party :class:`httpx_sse.ServerSentEvent` to callers.
    """

    async def parse(
            self,
            *args: Any,
            response_model: type[Any] | None = None,
            **kwargs: Any,
    ) -> Any:
        if response_model is None:
            return SSEEvent(
                event=self.event.event,
                data=self.event.data,
                id=self.event.id,
                retry=self.event.retry,
            )

        return pydantic.TypeAdapter(response_model).validate_json(self.event.data)


class JSONSSEEventClass(SSEEventClass[Any]):
    """Parses ``event.data`` as JSON and returns the raw decoded value."""

    async def parse(self, *args: Any, **kwargs: Any) -> Any:
        return self.event.json()


class PlainTextSSEEventClass(SSEEventClass[str]):
    """Returns ``event.data`` as a plain string."""

    async def parse(self, *args: Any, **kwargs: Any) -> str:
        return self.event.data
