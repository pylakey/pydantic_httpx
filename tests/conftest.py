"""Shared pytest fixtures.

We mock the network layer via httpx's official ``MockTransport`` — the same
recommendation given in the httpx docs (see ``docs/advanced/transports.md``).
That keeps the full request pipeline (cookies, redirects, content decoding)
intact while removing real I/O.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

import pydantic_httpx as ph

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def make_client():
    """Build a :class:`ph.Client` whose internal httpx client uses ``MockTransport``.

    The fixture returns a factory so tests can pass their own request handler
    and any extra ``Client(...)`` kwargs.
    """

    created: list[ph.Client] = []

    def factory(handler: Handler, **client_kwargs: Any) -> ph.Client:
        client = ph.Client(base_url="http://api.test", **client_kwargs)
        # Replace the internal httpx client; pass through any defaults the
        # Client wired up (headers/cookies/params/timeout/follow_redirects).
        original = client._client
        client._client = httpx.AsyncClient(
            base_url=str(original.base_url) or "http://api.test",
            headers=original.headers,
            cookies=original.cookies,
            params=original.params,
            timeout=original.timeout,
            follow_redirects=True,
            transport=httpx.MockTransport(handler),
        )
        created.append(client)
        return client

    yield factory

    # Best-effort cleanup of any clients that survived the test.
    for client in created:
        try:
            httpx_client = client._client
            if not httpx_client.is_closed:
                # ``aclose`` is async — we can't await here in a sync fixture.
                # Tests are expected to use the context manager or call ``close``;
                # this teardown is purely defensive.
                pass
        except Exception:
            pass


@pytest.fixture
def routes():
    """Tiny helper for building dict-routed handlers.

    Usage::

        def test_x(make_client, routes):
            handler = routes({
                ("GET", "/users/1"): lambda req: httpx.Response(200, json={"id": 1}),
            })
            client = make_client(handler)
    """

    def build(table: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]]) -> Handler:
        def handler(request: httpx.Request) -> httpx.Response:
            key = (request.method, request.url.path)
            route = table.get(key)
            if route is None:
                return httpx.Response(
                    500,
                    json={"error": "unmocked", "method": request.method, "path": request.url.path},
                )
            return route(request)

        return handler

    return build
