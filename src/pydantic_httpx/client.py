import asyncio
import logging
from collections.abc import AsyncIterator
from os import PathLike
from pathlib import Path
from typing import Any
from typing import TypeVar
from typing import cast

import httpx
import pydantic
from httpx_sse import aconnect_sse

from .encoders import encode_cookies
from .encoders import encode_headers
from .encoders import encode_params
from .encoders import to_jsonable
from .errors import HTTPError
from .errors import ResponseParseError
from .errors import errors_classes
from .responses import PydanticModelResponseClass
from .responses import ResponseClass
from .responses import SSEEventClass
from .responses import SSEPydanticEventClass
from .responses import StreamResponseClass
from .types import Body
from .types import Cookies
from .types import ErrorResponseModels
from .types import Headers
from .types import Params
from .utils import DEFAULT_DOWNLOAD_CHUNK_SIZE
from .utils import read_file_by_chunk

ResponseType = TypeVar('ResponseType')

PathLikeT = str | PathLike[str]

DEFAULT_TIMEOUT: float = 300.0


class Client:
    """Thin pydantic-aware wrapper around ``httpx.AsyncClient``.

    Highlights:
        * Accepts pydantic models for ``headers``/``cookies``/``params``/``body``.
        * Parses successful responses into a pydantic model via ``response_model``.
        * Maps HTTP error status codes to typed exceptions (``HTTPNotFound`` etc.).
        * Streams downloads to disk via :class:`StreamResponseClass`.
        * Streams Server-Sent Events via :meth:`sse` (async iterator).
    """

    def __init__(
            self,
            base_url: str = "",
            *,
            httpx_client: httpx.AsyncClient | None = None,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            error_response_models: ErrorResponseModels | None = None,
            bearer_token: str | pydantic.SecretStr | None = None,
            response_class: type[ResponseClass] = PydanticModelResponseClass,
            timeout: float = DEFAULT_TIMEOUT,
            follow_redirects: bool = True,
    ):
        """
        :param httpx_client: A fully pre-configured ``httpx.AsyncClient`` to use
            instead of letting this class build one. Useful for attaching
            ``event_hooks``, a custom ``transport``, ``httpx.Auth`` subclasses,
            ``http2=True``, custom ``Limits``, proxies, etc. When supplied,
            the user owns the client's lifecycle — :meth:`close` will NOT
            close it, and the conflicting transport-level kwargs ``base_url``,
            ``headers``, ``cookies``, ``params`` are rejected (configure them
            on the ``AsyncClient`` directly). ``timeout`` and
            ``follow_redirects`` are silently ignored in this mode.
        """
        self.logger = logging.getLogger("pydantic_httpx.Client")

        self._error_response_models: ErrorResponseModels = error_response_models or {}
        self._response_class: type[ResponseClass] = response_class

        if httpx_client is not None:
            conflicting = [
                name
                for name, value in (
                    ("base_url", base_url),
                    ("headers", headers),
                    ("cookies", cookies),
                    ("params", params),
                )
                if value
            ]
            if conflicting:
                raise ValueError(
                    f"Cannot combine httpx_client= with {conflicting}; "
                    "configure these on the httpx.AsyncClient instance directly."
                )
            self._client = httpx_client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=base_url or "",
                headers=encode_headers(headers) or {},
                cookies=encode_cookies(cookies) or {},
                params=encode_params(params) or {},
                timeout=timeout,
                follow_redirects=follow_redirects,
            )
            self._owns_client = True

        # bearer_token applies regardless of client ownership — it's auth,
        # not transport-level configuration.
        if bearer_token is not None:
            if isinstance(bearer_token, pydantic.SecretStr):
                bearer_token = bearer_token.get_secret_value()
            self._client.headers["Authorization"] = f"Bearer {bearer_token}"

    @property
    def httpx_client(self) -> httpx.AsyncClient:
        """Expose the underlying httpx client for advanced use cases."""
        return self._client

    async def _parse_response_error(
            self,
            response: httpx.Response,
            error_response_models: ErrorResponseModels | None = None,
    ) -> None:
        # Streaming responses (client.stream / aconnect_sse) may not be read yet.
        if not response.is_closed:
            await response.aread()

        merged_models = self._error_response_models | (error_response_models or {})
        error_class = errors_classes.get(response.status_code, HTTPError)
        error_response_model = merged_models.get(response.status_code)

        raw = response.content

        if error_response_model is not None:
            # User opted into a typed error payload — a parse/validation
            # failure here is a contract mismatch on their side, so surface
            # it explicitly instead of silently falling back to a typed
            # HTTP exception with a different shape.
            try:
                payload: Any = pydantic.TypeAdapter(error_response_model).validate_json(raw)
            except (pydantic.ValidationError, ValueError):
                raise ResponseParseError(raw_response=raw.decode(errors='replace'))
        else:
            # Best-effort decode. We never swallow the status-based exception
            # just because the body happens not to be JSON — a 404 with an
            # empty body must still raise `HTTPNotFound`, not `ResponseParseError`.
            try:
                payload = response.json()
            except ValueError:
                payload = response.text or None

        raise error_class(payload)

    def _build_request_kwargs(
            self,
            *,
            body: Any = None,
            data: Any = None,
            content: Any = None,
            files: Any = None,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            timeout: float | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}

        encoded_headers = encode_headers(headers)
        if encoded_headers:
            kwargs["headers"] = encoded_headers

        encoded_cookies = encode_cookies(cookies)
        if encoded_cookies:
            kwargs["cookies"] = encoded_cookies

        encoded_params = encode_params(params)
        if encoded_params:
            kwargs["params"] = encoded_params

        if body is not None:
            kwargs["json"] = to_jsonable(body, by_alias=True, exclude_unset=True)

        if data is not None:
            kwargs["data"] = data

        if content is not None:
            kwargs["content"] = content

        if files is not None:
            kwargs["files"] = files

        if timeout is not None:
            kwargs["timeout"] = timeout

        return kwargs

    async def request(
            self,
            method: str,
            path: str,
            *,
            body: Body | None = None,
            data: Any = None,
            content: Any = None,
            files: Any = None,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            response_model: type[ResponseType] | None = None,
            timeout: float | None = None,
            error_response_models: ErrorResponseModels | None = None,
            response_class: type[ResponseClass] | None = None,
            **response_class_parse_kwargs: Any,
    ) -> ResponseType | None:
        response_class = response_class or self._response_class

        if response_class is None:
            raise ValueError('response_class is not set')

        request_kwargs = self._build_request_kwargs(
            body=body,
            data=data,
            content=content,
            files=files,
            headers=headers,
            cookies=cookies,
            params=params,
            timeout=timeout,
        )

        if getattr(response_class, "streamed", False):
            async with self._client.stream(method, path, **request_kwargs) as response:
                if response.status_code >= 400:
                    await self._parse_response_error(response, error_response_models=error_response_models)

                return await response_class(response).parse(
                    response_model=response_model,
                    **response_class_parse_kwargs,
                )

        response = await self._client.request(method, path, **request_kwargs)

        if response.status_code >= 400:
            await self._parse_response_error(response, error_response_models=error_response_models)

        return await response_class(response).parse(
            response_model=response_model,
            **response_class_parse_kwargs,
        )

    async def sse(
            self,
            path: str,
            *,
            method: str = "GET",
            body: Body | None = None,
            data: Any = None,
            content: Any = None,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            response_model: type[ResponseType] | None = None,
            event_class: type[SSEEventClass] | None = None,
            timeout: float | None = None,
            error_response_models: ErrorResponseModels | None = None,
            **event_class_parse_kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Open a Server-Sent Events stream and yield parsed events.

        Default behavior: every event is parsed by :class:`SSEPydanticEventClass`.
        With ``response_model`` set, the iterator yields validated pydantic model
        instances built from ``event.data``. Without ``response_model``, it yields
        :class:`SSEEvent` — our own pydantic mirror of the SSE frame (``event``,
        ``data``, ``id``, ``retry``). The third-party
        :class:`httpx_sse.ServerSentEvent` is never leaked through the default
        path; pass ``event_class=RawSSEEventClass`` if you need it explicitly.

        Usage::

            async for chunk in client.sse('/v1/stream', method='POST',
                                          body=req, response_model=Chunk):
                ...
        """
        event_class = event_class or SSEPydanticEventClass

        request_kwargs = self._build_request_kwargs(
            body=body,
            data=data,
            content=content,
            files=None,
            headers=headers,
            cookies=cookies,
            params=params,
            timeout=timeout,
        )

        async with aconnect_sse(self._client, method, path, **request_kwargs) as event_source:
            response = event_source.response

            if response.status_code >= 400:
                await self._parse_response_error(response, error_response_models=error_response_models)

            async for raw_event in event_source.aiter_sse():
                yield await event_class(raw_event).parse(
                    response_model=response_model,
                    **event_class_parse_kwargs,
                )

    async def download_file(
            self,
            path: str,
            filepath: PathLikeT,
            *,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            timeout: float | None = None,
            error_response_models: ErrorResponseModels | None = None,
            chunk_size: int = DEFAULT_DOWNLOAD_CHUNK_SIZE,
    ) -> PathLikeT:
        result = await self.request(
            'GET',
            path,
            headers=headers,
            cookies=cookies,
            params=params,
            timeout=timeout,
            response_class=StreamResponseClass,
            error_response_models=error_response_models,
            filepath=filepath,
            chunk_size=chunk_size,
        )
        # StreamResponseClass always returns the filepath (or raises).
        return cast(PathLikeT, result)

    async def upload_file(
            self,
            path: str,
            file: PathLikeT,
            *,
            form_key: str = 'file',
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            response_model: type[ResponseType] | None = None,
            timeout: float | None = None,
            error_response_models: ErrorResponseModels | None = None,
            response_class: type[ResponseClass] | None = None,
    ) -> ResponseType | None:
        # Read the file in a worker thread so the event loop stays responsive —
        # the alternative (passing a sync file handle to httpx) would block the
        # loop during httpx's synchronous multipart serialization.
        # For files too large to hold in memory, use ``stream_file`` instead.
        file_path = Path(file)
        data = await asyncio.to_thread(file_path.read_bytes)
        return await self.request(
            'POST',
            path,
            # Pass (filename, bytes) so httpx preserves the original file name
            # in the multipart Content-Disposition header.
            files={form_key: (file_path.name, data)},
            headers=headers,
            cookies=cookies,
            params=params,
            response_model=response_model,
            timeout=timeout,
            error_response_models=error_response_models,
            response_class=response_class,
        )

    async def stream_file(
            self,
            path: str,
            file: PathLikeT,
            *,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            response_model: type[ResponseType] | None = None,
            timeout: float | None = None,
            error_response_models: ErrorResponseModels | None = None,
            response_class: type[ResponseClass] | None = None,
    ) -> ResponseType | None:
        return await self.request(
            'POST',
            path,
            content=read_file_by_chunk(file),
            headers=headers,
            cookies=cookies,
            params=params,
            response_model=response_model,
            timeout=timeout,
            error_response_models=error_response_models,
            response_class=response_class,
        )

    async def get(
            self,
            path: str,
            *,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            response_model: type[ResponseType] | None = None,
            timeout: float | None = None,
            error_response_models: ErrorResponseModels | None = None,
            response_class: type[ResponseClass] | None = None,
    ) -> ResponseType | None:
        return await self.request(
            "GET",
            path,
            headers=headers,
            cookies=cookies,
            params=params,
            response_model=response_model,
            timeout=timeout,
            error_response_models=error_response_models,
            response_class=response_class,
        )

    async def post(
            self,
            path: str,
            *,
            body: Body | None = None,
            data: Any = None,
            content: Any = None,
            files: Any = None,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            response_model: type[ResponseType] | None = None,
            timeout: float | None = None,
            error_response_models: ErrorResponseModels | None = None,
            response_class: type[ResponseClass] | None = None,
    ) -> ResponseType | None:
        return await self.request(
            "POST",
            path,
            body=body,
            data=data,
            content=content,
            files=files,
            headers=headers,
            cookies=cookies,
            params=params,
            response_model=response_model,
            timeout=timeout,
            error_response_models=error_response_models,
            response_class=response_class,
        )

    async def patch(
            self,
            path: str,
            *,
            body: Body | None = None,
            data: Any = None,
            content: Any = None,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            response_model: type[ResponseType] | None = None,
            timeout: float | None = None,
            error_response_models: ErrorResponseModels | None = None,
            response_class: type[ResponseClass] | None = None,
    ) -> ResponseType | None:
        return await self.request(
            "PATCH",
            path,
            body=body,
            data=data,
            content=content,
            headers=headers,
            cookies=cookies,
            params=params,
            response_model=response_model,
            timeout=timeout,
            error_response_models=error_response_models,
            response_class=response_class,
        )

    async def put(
            self,
            path: str,
            *,
            body: Body | None = None,
            data: Any = None,
            content: Any = None,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            response_model: type[ResponseType] | None = None,
            timeout: float | None = None,
            error_response_models: ErrorResponseModels | None = None,
            response_class: type[ResponseClass] | None = None,
    ) -> ResponseType | None:
        return await self.request(
            "PUT",
            path,
            body=body,
            data=data,
            content=content,
            headers=headers,
            cookies=cookies,
            params=params,
            response_model=response_model,
            timeout=timeout,
            error_response_models=error_response_models,
            response_class=response_class,
        )

    async def delete(
            self,
            path: str,
            *,
            body: Body | None = None,
            data: Any = None,
            content: Any = None,
            headers: Headers | None = None,
            cookies: Cookies | None = None,
            params: Params | None = None,
            response_model: type[ResponseType] | None = None,
            timeout: float | None = None,
            error_response_models: ErrorResponseModels | None = None,
            response_class: type[ResponseClass] | None = None,
    ) -> ResponseType | None:
        return await self.request(
            "DELETE",
            path,
            body=body,
            data=data,
            content=content,
            headers=headers,
            cookies=cookies,
            params=params,
            response_model=response_model,
            timeout=timeout,
            error_response_models=error_response_models,
            response_class=response_class,
        )

    async def close(self) -> None:
        # User-supplied httpx clients stay alive — the caller owns them.
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
