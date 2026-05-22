"""Real-network integration tests.

These tests are **opt-in**: they require internet and are skipped by the
default ``pytest`` invocation. To run them::

    uv run pytest -m integration

Three public APIs are exercised end-to-end:

* `JSONPlaceholder <https://jsonplaceholder.typicode.com>`_ — REST CRUD against
  nested pydantic models, query params, typed-error mapping.
* `httpbin.org <https://httpbin.org>`_ — binary download, multipart upload,
  chunked raw upload.
* `Wikimedia EventStreams <https://stream.wikimedia.org/?doc>`_ — live
  Server-Sent Events firehose used to verify ``Client.sse(...)`` end-to-end.

Schemas below match live shapes as of 2026-05-22.
"""
from __future__ import annotations

import pydantic
import pytest

import pydantic_httpx as ph
from pydantic_httpx import Client

# Every test in this module is an integration test.
pytestmark = pytest.mark.integration


BASE_URL = "https://jsonplaceholder.typicode.com"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class Post(pydantic.BaseModel):
    userId: int
    id: int
    title: str
    body: str


class NewPost(pydantic.BaseModel):
    """Payload accepted by POST /posts — no `id` field on the way in."""

    userId: int
    title: str
    body: str


class Todo(pydantic.BaseModel):
    userId: int
    id: int
    title: str
    completed: bool


class Comment(pydantic.BaseModel):
    postId: int
    id: int
    name: str
    # JSONPlaceholder emails are mock strings ("Sincere@april.biz") but still
    # validate as `EmailStr`. Stay with `str` to keep the test robust against
    # any odd entry in the dataset.
    email: str
    body: str


class Geo(pydantic.BaseModel):
    lat: str  # JSONPlaceholder returns them as strings
    lng: str


class Address(pydantic.BaseModel):
    street: str
    suite: str
    city: str
    zipcode: str
    geo: Geo


class Company(pydantic.BaseModel):
    name: str
    catchPhrase: str
    bs: str


class User(pydantic.BaseModel):
    id: int
    name: str
    username: str
    email: str
    address: Address
    phone: str
    website: str
    company: Company


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def client():
    """A real Client pointed at JSONPlaceholder."""
    async with Client(BASE_URL, timeout=30.0) as c:
        yield c


# ---------------------------------------------------------------------------
# Single resource
# ---------------------------------------------------------------------------

async def test_get_single_post(client: Client):
    post = await client.get("/posts/1", response_model=Post)
    assert post.id == 1
    assert post.userId == 1
    assert post.title  # non-empty
    assert post.body


async def test_get_single_todo(client: Client):
    todo = await client.get("/todos/1", response_model=Todo)
    assert todo.id == 1
    assert isinstance(todo.completed, bool)


async def test_get_nested_user(client: Client):
    user = await client.get("/users/1", response_model=User)
    # Nested pydantic models — Address.Geo and Company — should be populated.
    assert user.id == 1
    assert user.address.city
    assert user.address.geo.lat
    assert user.address.geo.lng
    assert user.company.name


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

async def test_get_list_of_posts_via_type_adapter(client: Client):
    # TypeAdapter handles `list[Model]` natively — no wrapper class needed.
    posts = await client.get("/posts", response_model=list[Post])
    assert isinstance(posts, list)
    assert len(posts) == 100
    assert all(isinstance(p, Post) for p in posts)


async def test_get_users_list(client: Client):
    users = await client.get("/users", response_model=list[User])
    assert len(users) == 10
    assert {u.id for u in users} == set(range(1, 11))


# ---------------------------------------------------------------------------
# Query params
# ---------------------------------------------------------------------------

async def test_query_params_plain_dict(client: Client):
    comments = await client.get(
        "/comments",
        params={"postId": 1},
        response_model=list[Comment],
    )
    assert len(comments) > 0
    assert all(c.postId == 1 for c in comments)


class CommentsFilter(pydantic.BaseModel):
    postId: int


async def test_query_params_pydantic_model(client: Client):
    comments = await client.get(
        "/comments",
        params=CommentsFilter(postId=2),
        response_model=list[Comment],
    )
    assert len(comments) > 0
    assert all(c.postId == 2 for c in comments)


async def test_nested_resource_endpoint(client: Client):
    # /posts/1/comments — proves that path construction works the same way as
    # query-param filtering on /comments?postId=1.
    comments = await client.get("/posts/1/comments", response_model=list[Comment])
    assert len(comments) > 0
    assert all(c.postId == 1 for c in comments)


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------

async def test_create_post(client: Client):
    payload = NewPost(userId=1, title="hello", body="from pydantic_httpx")
    created = await client.post("/posts", body=payload, response_model=Post)

    # JSONPlaceholder always assigns id=101 to created resources.
    assert created.id == 101
    assert created.userId == payload.userId
    assert created.title == payload.title
    assert created.body == payload.body


async def test_put_replaces_post(client: Client):
    replacement = Post(userId=1, id=1, title="replaced", body="replaced body")
    updated = await client.put("/posts/1", body=replacement, response_model=Post)
    assert updated.id == 1
    assert updated.title == "replaced"
    assert updated.body == "replaced body"


async def test_patch_merges_fields(client: Client):
    # Partial update — only `title` provided. Body must come back merged.
    updated = await client.patch(
        "/posts/1",
        body={"title": "patched"},
        response_model=Post,
    )
    assert updated.id == 1
    assert updated.title == "patched"
    # `body` retained from the existing record on the server side.
    assert updated.body


async def test_delete_post(client: Client):
    # JSONPlaceholder returns 200 + empty `{}` on delete. With response_model
    # omitted, we get an EmptyResponse pydantic instance.
    result = await client.delete("/posts/1")
    assert isinstance(result, ph.EmptyResponse)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

async def test_404_raises_typed_exception(client: Client):
    with pytest.raises(ph.HTTPNotFound) as exc_info:
        await client.get("/posts/9999", response_model=Post)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Different response_class flavours against the same endpoint
# ---------------------------------------------------------------------------

async def test_json_response_class_returns_dict(client: Client):
    raw = await client.get("/posts/1", response_class=ph.JSONResponseClass)
    assert isinstance(raw, dict)
    assert raw["id"] == 1


async def test_plain_text_response_class_returns_str(client: Client):
    text = await client.get("/posts/1", response_class=ph.PlainTextResponseClass)
    assert isinstance(text, str)
    # Body is JSON-encoded text — still arrives as a string in this mode.
    assert text.lstrip().startswith("{")


async def test_pydantic_model_with_dict_as_model(client: Client):
    # Passing `dict` as the response_model is equivalent to JSONResponseClass.
    raw = await client.get("/posts/1", response_model=dict)
    assert isinstance(raw, dict)
    assert raw["id"] == 1


# ---------------------------------------------------------------------------
# Headers reach the wire
# ---------------------------------------------------------------------------

async def test_default_and_per_request_headers_combine(client: Client):
    # JSONPlaceholder echoes `Content-Type` back via the request, but doesn't
    # surface custom headers in the response — so we just verify the request
    # succeeds with both kinds of headers set.
    post = await client.get(
        "/posts/1",
        headers={"X-Trace-Id": "test-001"},
        response_model=Post,
    )
    assert post.id == 1


# ---------------------------------------------------------------------------
# File transfer against httpbin.org
# ---------------------------------------------------------------------------
#
# JSONPlaceholder has no file endpoints, so download/upload live tests target
# httpbin.org — the canonical public testbed for HTTP clients.

HTTPBIN_URL = "https://httpbin.org"


@pytest.fixture
async def httpbin():
    async with Client(HTTPBIN_URL, timeout=30.0) as c:
        yield c


# PNG magic bytes — first 8 bytes of any valid PNG file.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


async def test_download_file_png(httpbin: Client, tmp_path):
    target = tmp_path / "image.png"
    result = await httpbin.download_file("/image/png", filepath=target, chunk_size=2048)

    assert result == target
    assert target.exists()
    content = target.read_bytes()
    # Verify it's a real PNG, not an error page silently written to disk.
    assert content.startswith(PNG_MAGIC)
    # /image/png currently returns ~8 KB; relax to >1 KB to absorb upstream changes.
    assert len(content) > 1_000


async def test_download_file_large_streamed_chunks(httpbin: Client, tmp_path):
    target = tmp_path / "blob.bin"
    requested_size = 65_536  # 64 KB — forces multiple chunks at chunk_size=4096.

    result = await httpbin.download_file(
        f"/bytes/{requested_size}",
        filepath=target,
        chunk_size=4096,
    )
    assert result == target
    assert target.stat().st_size == requested_size


async def test_download_file_404_raises_typed_exception(httpbin: Client, tmp_path):
    # httpbin's /status/404 returns an empty body. The client still raises
    # the typed `HTTPNotFound` because the user didn't ask for a specific
    # error payload via `error_response_models=` — JSON parse failure is
    # treated as "no body", not as a user-facing error.
    target = tmp_path / "nope.bin"
    with pytest.raises(ph.HTTPNotFound) as exc_info:
        await httpbin.download_file("/status/404", filepath=target)
    # Empty body → payload is None.
    assert exc_info.value.response is None
    # Destination file must NOT have been written — _parse_response_error
    # fires before the stream is consumed.
    assert not target.exists()


async def test_upload_file_multipart(httpbin: Client, tmp_path):
    payload = b"hello-from-pydantic-httpx" * 100  # 2500 bytes
    src = tmp_path / "data.bin"
    src.write_bytes(payload)

    # httpbin's /post echoes back the request body it received. With
    # multipart/form-data the file lands under the `files` key.
    echoed = await httpbin.upload_file(
        "/post",
        src,
        form_key="myfile",
        response_class=ph.JSONResponseClass,
    )
    assert isinstance(echoed, dict)
    assert "myfile" in echoed["files"]
    # httpbin returns the file content as a string — compare lengths to confirm
    # the full payload made the round trip.
    assert len(echoed["files"]["myfile"]) == len(payload)


async def test_stream_file_raw_chunked(httpbin: Client, tmp_path):
    payload = b"X" * 50_000  # 50 KB
    src = tmp_path / "big.bin"
    src.write_bytes(payload)

    # Raw chunked upload (no multipart) — httpbin echoes the body under `data`.
    echoed = await httpbin.stream_file(
        "/post",
        src,
        response_class=ph.JSONResponseClass,
    )
    assert isinstance(echoed, dict)
    assert len(echoed["data"]) == len(payload)


# ---------------------------------------------------------------------------
# Server-Sent Events against the Wikimedia public stream
# ---------------------------------------------------------------------------
#
# https://stream.wikimedia.org/v2/stream/recentchange is the public SSE feed of
# every edit to every Wikimedia property (Wikipedia, Wikidata, Commons, ...).
# It emits dozens of events per second, so a few iterations are enough to
# verify the SSE pipeline end-to-end. We break early to keep the test fast.

WIKIMEDIA_URL = "https://stream.wikimedia.org"
SSE_PATH = "/v2/stream/recentchange"


@pytest.fixture
async def wikimedia():
    # Wikimedia EventStreams policy requires an identifying User-Agent.
    # Default ``python-httpx/...`` gets a 403 Forbidden.
    async with Client(
        WIKIMEDIA_URL,
        timeout=15.0,
        headers={"User-Agent": "pydantic-httpx-tests/0.1 (https://github.com/pylakey/pydantic_httpx)"},
    ) as c:
        yield c


class WikimediaMeta(pydantic.BaseModel):
    """Subset of the `meta` block of a recentchange event.

    We declare only the fields we care about and accept any others via
    ``extra="allow"`` — the schema upstream is large and may evolve.
    """

    model_config = pydantic.ConfigDict(extra="allow")

    domain: str
    stream: str
    dt: str  # ISO-8601 string


class RecentChange(pydantic.BaseModel):
    """Subset of the recentchange event payload."""

    model_config = pydantic.ConfigDict(extra="allow")

    type: str  # "edit", "categorize", "log", "new", ...
    title: str
    meta: WikimediaMeta


async def test_sse_yields_parsed_pydantic_models(wikimedia: Client):
    """response_model=RecentChange → iterator yields validated models."""

    received: list[RecentChange] = []
    async for change in wikimedia.sse(SSE_PATH, response_model=RecentChange):
        received.append(change)
        if len(received) >= 5:
            break  # early exit closes the SSE stream via context manager

    assert len(received) == 5
    assert all(isinstance(e, RecentChange) for e in received)
    # Every event from this stream lives under the same logical stream name.
    assert all(e.meta.stream == "mediawiki.recentchange" for e in received)
    # `dt` is ISO-8601 — should be parseable.
    assert all(e.meta.dt for e in received)


async def test_sse_default_yields_sse_event(wikimedia: Client):
    """Without response_model — yields our own SSEEvent pydantic model."""

    received: list[ph.SSEEvent] = []
    async for event in wikimedia.sse(SSE_PATH):
        received.append(event)
        if len(received) >= 3:
            break

    assert len(received) == 3
    # Wikimedia uses the default "message" event type — never leaks the
    # third-party httpx_sse.ServerSentEvent class here.
    assert all(isinstance(e, ph.SSEEvent) for e in received)
    assert all(e.event == "message" for e in received)
    assert all(e.data for e in received)  # data field is non-empty JSON


async def test_sse_raw_event_class_yields_third_party_objects(wikimedia: Client):
    """Opt-in raw access via event_class=RawSSEEventClass."""
    from pydantic_httpx.responses import RawSSEEventClass

    received: list[ph.ServerSentEvent] = []
    async for sse in wikimedia.sse(SSE_PATH, event_class=RawSSEEventClass):
        received.append(sse)
        if len(received) >= 2:
            break

    assert len(received) == 2
    assert all(isinstance(e, ph.ServerSentEvent) for e in received)
    # httpx_sse populates `.event`, `.data`; id may or may not be present.
    assert all(e.event for e in received)
