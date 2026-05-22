"""Tests for the typed HTTP error hierarchy."""
from __future__ import annotations

import http

import pydantic
import pytest

import pydantic_httpx as ph
from pydantic_httpx.errors import HTTPClientError
from pydantic_httpx.errors import HTTPRedirect
from pydantic_httpx.errors import HTTPServerError
from pydantic_httpx.errors import errors_classes


@pytest.mark.parametrize(
    "status,expected",
    [
        (http.HTTPStatus.MOVED_PERMANENTLY, ph.HTTPMovedPermanently),
        (http.HTTPStatus.BAD_REQUEST, ph.HTTPBadRequest),
        (http.HTTPStatus.UNAUTHORIZED, ph.HTTPUnauthorized),
        (http.HTTPStatus.FORBIDDEN, ph.HTTPForbidden),
        (http.HTTPStatus.NOT_FOUND, ph.HTTPNotFound),
        (http.HTTPStatus.IM_A_TEAPOT, ph.HTTPImATeapot),
        (http.HTTPStatus.UNPROCESSABLE_ENTITY, ph.HTTPUnprocessableEntity),
        (http.HTTPStatus.TOO_MANY_REQUESTS, ph.HTTPTooManyRequests),
        (http.HTTPStatus.INTERNAL_SERVER_ERROR, ph.HTTPInternalServerError),
        (http.HTTPStatus.BAD_GATEWAY, ph.HTTPBadGateway),
    ],
)
def test_errors_classes_mapping(status, expected):
    assert errors_classes[status] is expected
    assert errors_classes[status].status_code == status


def test_error_class_hierarchy():
    """3xx → HTTPRedirect, 4xx → HTTPClientError, 5xx → HTTPServerError, all → HTTPError."""
    assert issubclass(ph.HTTPMovedPermanently, HTTPRedirect)
    assert issubclass(ph.HTTPNotFound, HTTPClientError)
    assert issubclass(ph.HTTPInternalServerError, HTTPServerError)
    for cls in (HTTPRedirect, HTTPClientError, HTTPServerError):
        assert issubclass(cls, ph.HTTPError)


def test_http_error_carries_response_payload():
    payload = {"code": "x", "detail": "y"}
    exc = ph.HTTPNotFound(payload)
    assert exc.response == payload
    assert exc.status_code == http.HTTPStatus.NOT_FOUND


def test_response_parse_error_keeps_raw():
    raw = "not json"
    err = ph.ResponseParseError(raw_response=raw)
    assert err.raw_response == raw


def test_error_payload_can_be_pydantic_model():
    class ApiError(pydantic.BaseModel):
        detail: str

    exc = ph.HTTPNotFound(ApiError(detail="missing"))
    assert isinstance(exc.response, ApiError)
    assert exc.response.detail == "missing"


def test_errors_classes_covers_3xx_4xx_5xx_ranges():
    # Sanity check that we have entries across all three families.
    families = {300: False, 400: False, 500: False}
    for code in errors_classes:
        families[(int(code) // 100) * 100] = True
    assert all(families.values())
