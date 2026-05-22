# pydantic_httpx - Symbiosis of [Pydantic](https://github.com/pydantic/pydantic), [HTTPX](https://www.python-httpx.org) and [httpx-sse](https://github.com/florimondmanca/httpx-sse)

A small, fully-async HTTP client that pairs `httpx` with `pydantic v2`: pass models in, get models out, with first-class Server-Sent Events support via `httpx-sse`.

It is the successor of [pydantic_aiohttp](https://github.com/pylakey/pydantic_aiohttp) — same ergonomics, but built on top of `httpx` and with native SSE streaming.

## Features

* Pydantic models accepted **everywhere** the wire takes data — `body`, `params`, `headers`, `cookies`.
* Successful responses parsed into a model via `response_model=...` using `pydantic.TypeAdapter.validate_json` (one pass, no intermediate `json.loads`).
* Typed exceptions for every standard HTTP status (`HTTPNotFound`, `HTTPUnauthorized`, ...) and per-status pydantic error payloads via `error_response_models`.
* **Server-Sent Events** as an `async for` iterator — yields validated pydantic models when `response_model=` is set.
* Streaming file download / chunked upload via `aiofiles`.
* Pluggable parsers — `ResponseClass` for whole-response parsing, `SSEEventClass` for individual SSE frames.
* Modern Python: targets 3.11+, uses PEP 604 / PEP 585 syntax, no `typing.Optional`/`Union`/`List`/`Dict`.

## Installation

```bash
pip install pydantic-httpx
# or
uv add pydantic-httpx
```

## Examples

### Basic example

```python
import asyncio

import pydantic

from pydantic_httpx import Client
from pydantic_httpx.responses import (
    JSONResponseClass,
    PlainTextResponseClass,
    PydanticModelResponseClass,
)


class Todo(pydantic.BaseModel):
    userId: int
    id: int
    title: str
    completed: bool


async def main():
    client = Client('https://jsonplaceholder.typicode.com')

    async with client:
        # Plain text response
        todo = await client.get('/todos/1', response_class=PlainTextResponseClass)
        print(isinstance(todo, str))  # True

        # Bare JSON dict
        todo = await client.get('/todos/1', response_class=JSONResponseClass)
        print(isinstance(todo, dict))  # True

        # Validated pydantic model
        todo = await client.get(
            '/todos/1',
            response_class=PydanticModelResponseClass,
            response_model=Todo,
        )
        print(isinstance(todo, Todo))  # True

        # PydanticModelResponseClass is the default — `response_class` can be omitted
        todo = await client.get('/todos/1', response_model=Todo)
        print(isinstance(todo, Todo))  # True


if __name__ == '__main__':
    asyncio.run(main())
```

### Explicit close vs context manager

```python
import asyncio

import pydantic
from pydantic_httpx import Client


class Todo(pydantic.BaseModel):
    userId: int
    id: int
    title: str
    completed: bool


async def with_context_manager():
    # Preferred — auto-closes the underlying httpx.AsyncClient.
    async with Client('https://jsonplaceholder.typicode.com') as client:
        await client.get('/todos/1', response_model=Todo)


async def with_explicit_close():
    client = Client('https://jsonplaceholder.typicode.com')
    try:
        await client.get('/todos/1', response_model=Todo)
    finally:
        await client.close()


asyncio.run(with_context_manager())
```

### Query params via pydantic models

```python
import pydantic
from pydantic_httpx import Client


class CommentsFilter(pydantic.BaseModel):
    postId: int
    # Default values that aren't explicitly set are dropped (exclude_unset=True).
    # Booleans are serialized as lowercase "true" / "false".
    only_unread: bool = False


class Comment(pydantic.BaseModel):
    postId: int
    id: int
    name: str
    email: str
    body: str


async def fetch_comments():
    async with Client('https://jsonplaceholder.typicode.com') as client:
        comments = await client.get(
            '/comments',
            params=CommentsFilter(postId=1),
            response_model=list[Comment],
        )
        for c in comments[:3]:
            print(c.email)
```

### Sending a body

```python
import pydantic
from pydantic_httpx import Client


class NewPost(pydantic.BaseModel):
    userId: int
    title: str
    body: str


class Post(NewPost):
    id: int


async def create_post():
    async with Client('https://jsonplaceholder.typicode.com') as client:
        post = await client.post(
            '/posts',
            body=NewPost(userId=1, title='hello', body='world'),
            response_model=Post,
        )
        print(post.id)  # 101
```

### Bring your own `httpx.AsyncClient`

Need event hooks, a custom transport, `httpx.Auth`, `http2=True`, custom
`Limits`, or proxy config? Build the `httpx.AsyncClient` yourself and hand
it to `Client(httpx_client=...)`.

```python
import httpx
from pydantic_httpx import Client


async def on_request(request: httpx.Request) -> None:
    print(f"-> {request.method} {request.url}")


async def on_response(response: httpx.Response) -> None:
    print(f"<- {response.status_code} {response.request.url}")


external = httpx.AsyncClient(
    base_url='https://api.example.com',
    http2=True,
    event_hooks={'request': [on_request], 'response': [on_response]},
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)

async def main():
    # We do NOT close the user-supplied client — caller owns its lifecycle.
    async with Client(httpx_client=external) as client:
        result = await client.get('/v1/things', response_model=list[Thing])
    # external is still alive here
    await external.aclose()
```

When `httpx_client=` is provided, the transport-level kwargs
(`base_url`, `headers`, `cookies`, `params`) are **rejected** with a clear
`ValueError` — configure them on the `AsyncClient` itself. `bearer_token=`
still works (it's auth, not transport config); `response_class=` /
`error_response_models=` / `timeout=` / `follow_redirects=` are
pydantic_httpx-level concerns and behave as usual (the last two are
ignored when you bring your own client).

### Bearer-token authentication

```python
import pydantic
from pydantic_httpx import Client

# A plain str or pydantic.SecretStr — both are accepted.
client = Client(
    'https://api.example.com',
    bearer_token=pydantic.SecretStr('my-token'),
)
# Every request now carries `Authorization: Bearer my-token`.
```

### Handling errors parsed as pydantic models

```python
import http
import asyncio

import pydantic

import pydantic_httpx
from pydantic_httpx import Client


class FastAPIValidationError(pydantic.BaseModel):
    loc: list[str]
    msg: str
    type: str


class FastAPIUnprocessableEntityError(pydantic.BaseModel):
    detail: list[FastAPIValidationError]


class User(pydantic.BaseModel):
    id: str
    email: str
    first_name: str
    last_name: str
    is_admin: bool


async def main():
    client = Client(
        'https://fastapi.example.com',
        error_response_models={
            http.HTTPStatus.UNPROCESSABLE_ENTITY: FastAPIUnprocessableEntityError,
        },
    )

    try:
        await client.post(
            '/users',
            body={'first_name': 'John', 'last_name': 'Doe'},  # email missing
            response_model=User,
        )
    except pydantic_httpx.HTTPUnprocessableEntity as e:
        # e.response is now a validated FastAPIUnprocessableEntityError instance
        print(e.response.detail[0].model_dump_json(indent=4))
    finally:
        await client.close()


asyncio.run(main())
```

Per-request `error_response_models=` override the client-wide defaults for that
specific call. If the body cannot be parsed against the model (or isn't JSON
at all), `pydantic_httpx.ResponseParseError` is raised with the raw body
preserved on `.raw_response`.

### Downloading files

```python
import asyncio

from pydantic_httpx import Client


async def main():
    async with Client('https://httpbin.org') as client:
        # Streams the response body straight to disk via httpx.AsyncClient.stream
        # + aiofiles — the full payload is never held in memory.
        filepath = await client.download_file(
            '/image/png',
            filepath='downloaded.png',
            chunk_size=64 * 1024,
        )
        print(filepath)


asyncio.run(main())
```

### Uploading files

```python
from pydantic_httpx import Client

async def upload():
    async with Client('https://api.example.com') as client:
        # multipart/form-data upload
        await client.upload_file('/upload', '/path/to/file.bin', form_key='file')

        # streamed (chunked) raw upload — no multipart framing
        await client.stream_file('/upload-raw', '/path/to/file.bin')
```

### Server-Sent Events

`Client.sse(...)` returns an async iterator and never leaks the third-party
`httpx_sse.ServerSentEvent` type by default — it yields either the pydantic
`SSEEvent` mirror or a validated `response_model` instance.

#### Default — yields `SSEEvent` pydantic models

```python
from pydantic_httpx import Client, SSEEvent


async def consume():
    async with Client('https://api.example.com') as client:
        async for event in client.sse('/v1/events'):
            assert isinstance(event, SSEEvent)
            print(event.event, event.data, event.id, event.retry)
```

#### With `response_model` — yields validated models

```python
import pydantic
from pydantic_httpx import Client


class StreamRequest(pydantic.BaseModel):
    prompt: str
    stream: bool = True


class Chunk(pydantic.BaseModel):
    delta: str
    index: int


async def stream_chunks():
    async with Client('https://api.example.com') as client:
        async for chunk in client.sse(
            '/v1/chat/completions',
            method='POST',
            body=StreamRequest(prompt='hi'),
            response_model=Chunk,
        ):
            print(chunk.delta, end='', flush=True)
```

`response_model` is forwarded to `pydantic.TypeAdapter.validate_json(event.data)`,
so anything `TypeAdapter` accepts works — `BaseModel`, `list[Model]`, dataclass,
`dict[str, Foo]`, etc.

#### Opt-in raw access — `RawSSEEventClass`

```python
from pydantic_httpx import Client, ServerSentEvent
from pydantic_httpx.responses import RawSSEEventClass


async def raw_events():
    async with Client('https://api.example.com') as client:
        async for sse in client.sse('/v1/events', event_class=RawSSEEventClass):
            assert isinstance(sse, ServerSentEvent)
            print(sse.event, sse.data, sse.id, sse.retry)
```

#### Other built-in event parsers

| Class                    | Yields                          | Notes                                  |
|--------------------------|---------------------------------|----------------------------------------|
| `SSEPydanticEventClass`  | `SSEEvent` *or* `response_model` | Default — recommended.                 |
| `RawSSEEventClass`       | `httpx_sse.ServerSentEvent`     | Opt-in raw access.                     |
| `JSONSSEEventClass`      | `Any` (parsed JSON)             | When you want a dict without a schema. |
| `PlainTextSSEEventClass` | `str`                           | Returns `event.data` verbatim.         |

#### Live demo: Wikimedia EventStreams

A working end-to-end SSE example you can run without any API keys —
[stream.wikimedia.org](https://stream.wikimedia.org/?doc) publishes every
Wikipedia/Wikidata edit as a live SSE feed:

```python
import asyncio
import pydantic

from pydantic_httpx import Client


class Meta(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra='allow')
    domain: str
    stream: str
    dt: str


class RecentChange(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra='allow')
    type: str
    title: str
    meta: Meta


async def main():
    # Wikimedia requires an identifying User-Agent.
    async with Client(
        'https://stream.wikimedia.org',
        headers={'User-Agent': 'my-app/0.1 (contact@example.com)'},
    ) as client:
        async for change in client.sse('/v2/stream/recentchange', response_model=RecentChange):
            print(f"[{change.meta.domain}] {change.type}: {change.title}")


asyncio.run(main())
```

### Custom response_class / event_class

Both `ResponseClass` and `SSEEventClass` are tiny abstract base classes — you
can plug your own parser if the built-ins don't fit.

```python
import abc
import csv
from io import StringIO

import httpx

from pydantic_httpx.responses import ResponseClass


class CSVResponseClass(ResponseClass[list[dict[str, str]]]):
    async def parse(self, *args, **kwargs) -> list[dict[str, str]]:
        reader = csv.DictReader(StringIO(self.response.text))
        return list(reader)


# usage
async def main():
    async with Client('https://api.example.com') as client:
        rows = await client.get('/data.csv', response_class=CSVResponseClass)
```

## Quick reference

```python
Client(
    base_url='',
    *,
    httpx_client=None,     # httpx.AsyncClient — bring your own for hooks/transports/etc.
    headers=None,          # dict | pydantic model
    cookies=None,          # dict | pydantic model
    params=None,           # dict | pydantic model
    error_response_models=None,    # {status: pydantic model}
    bearer_token=None,     # str | pydantic.SecretStr
    response_class=PydanticModelResponseClass,
    timeout=300.0,         # ignored when httpx_client is provided
    follow_redirects=True, # ignored when httpx_client is provided
)

# Per-request methods (all async, all accept the same kwargs):
client.get / post / put / patch / delete(
    path,
    *,
    body=None,             # dict | pydantic model            (json= on the wire)
    data=None,             # dict | str | bytes               (data= form on the wire)
    content=None,          # bytes | iterator                 (raw content=)
    files=None,            # for multipart                    (files=)
    headers=None, cookies=None, params=None,
    response_model=None,
    response_class=None,
    error_response_models=None,
    timeout=None,
)

client.download_file(path, filepath, *, chunk_size=64*1024, ...)
client.upload_file(path, file, *, form_key='file', ...)
client.stream_file(path, file, ...)

client.sse(
    path,
    *,
    method='GET',
    body=None, data=None, content=None,
    headers=None, cookies=None, params=None,
    response_model=None,
    event_class=None,
    error_response_models=None,
    timeout=None,
)
```

## Testing

The project ships with an extensive test suite (~140 tests, 99% coverage on the
unit layer). The unit suite mocks the network through the
[official `httpx.MockTransport` recommendation](https://www.python-httpx.org/advanced/transports/)
— the full client pipeline (cookies, redirects, content decoding) is still
exercised, just without real I/O.

```bash
# unit tests only (no network) — ~1s
uv run pytest

# opt-in integration tests against real public APIs
# (JSONPlaceholder for REST CRUD, httpbin.org for file transfer,
#  Wikimedia EventStreams for SSE) — ~25s, requires internet
uv run pytest -m integration
```

## License

This project is licensed under the terms of the MIT license.
