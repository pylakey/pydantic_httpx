"""Tests for the JSON / URL encoders."""
from __future__ import annotations

import datetime
import enum
import uuid
from decimal import Decimal

import pydantic
import pytest

from pydantic_httpx.encoders import encode_cookies
from pydantic_httpx.encoders import encode_headers
from pydantic_httpx.encoders import encode_params
from pydantic_httpx.encoders import to_jsonable


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"


class Sample(pydantic.BaseModel):
    """Covers the value types pydantic v2 needs to handle on its own."""

    name: str
    count: int = 0
    when: datetime.datetime | None = None
    uid: uuid.UUID | None = None
    color: Color | None = None
    price: Decimal | None = None
    secret: pydantic.SecretStr | None = None


# ---------------------------------------------------------------------------
# to_jsonable
# ---------------------------------------------------------------------------

class TestToJsonable:
    def test_none(self):
        assert to_jsonable(None) is None

    @pytest.mark.parametrize("value", ["text", 42, 3.14, True, False])
    def test_primitives_pass_through(self, value):
        assert to_jsonable(value) == value

    def test_basemodel_full_field_coverage(self):
        moment = datetime.datetime(2026, 5, 22, 10, 0, 0, tzinfo=datetime.timezone.utc)
        uid = uuid.UUID("00000000-0000-0000-0000-000000000001")
        m = Sample(
            name="alice",
            count=3,
            when=moment,
            uid=uid,
            color=Color.RED,
            price=Decimal("1.50"),
            secret=pydantic.SecretStr("topsecret"),
        )

        result = to_jsonable(m)

        # exclude_unset is True by default — defaults stay unless explicitly set.
        assert result == {
            "name": "alice",
            "count": 3,
            "when": "2026-05-22T10:00:00Z",
            "uid": "00000000-0000-0000-0000-000000000001",
            "color": "red",
            "price": "1.50",
            "secret": "**********",
        }

    def test_basemodel_exclude_unset_default_skips_defaults(self):
        m = Sample(name="bob")
        # `count` has a default and was not set explicitly.
        assert to_jsonable(m) == {"name": "bob"}

    def test_basemodel_exclude_unset_false_includes_defaults(self):
        m = Sample(name="bob")
        result = to_jsonable(m, exclude_unset=False)
        assert result["name"] == "bob"
        assert result["count"] == 0
        # Optional fields default to None and should be included.
        assert result["when"] is None

    def test_basemodel_exclude_none(self):
        m = Sample(name="bob", when=None)  # explicitly set to None
        result = to_jsonable(m, exclude_none=True)
        assert "when" not in result

    def test_basemodel_by_alias(self):
        class Aliased(pydantic.BaseModel):
            value: int = pydantic.Field(alias="v")

        m = Aliased(v=5)
        assert to_jsonable(m, by_alias=True) == {"v": 5}
        assert to_jsonable(m, by_alias=False) == {"value": 5}

    def test_nested_dict_with_models(self):
        m = Sample(name="alice")
        result = to_jsonable({"wrapper": m, "other": 1})
        assert result == {"wrapper": {"name": "alice"}, "other": 1}

    def test_exclude_none_in_dict(self):
        result = to_jsonable({"a": 1, "b": None, "c": 3}, exclude_none=True)
        assert result == {"a": 1, "c": 3}

    @pytest.mark.parametrize(
        "collection",
        [
            [1, 2, 3],
            (1, 2, 3),
            {1, 2, 3},
            frozenset({1, 2, 3}),
        ],
    )
    def test_collections_become_lists(self, collection):
        result = to_jsonable(collection)
        assert isinstance(result, list)
        assert sorted(result) == [1, 2, 3]

    def test_list_of_models(self):
        models = [Sample(name="a"), Sample(name="b")]
        assert to_jsonable(models) == [{"name": "a"}, {"name": "b"}]


# ---------------------------------------------------------------------------
# encode_params
# ---------------------------------------------------------------------------

class TestEncodeParams:
    def test_none(self):
        assert encode_params(None) is None

    def test_non_dict_returns_none(self):
        # Pydantic models that dump to scalars/lists would land here.
        assert encode_params([1, 2, 3]) is None

    def test_bool_lowercased(self):
        result = encode_params({"active": True, "deleted": False})
        assert result == {"active": "true", "deleted": "false"}

    def test_drops_none_values(self):
        result = encode_params({"a": 1, "b": None})
        assert result == {"a": 1}

    def test_pydantic_model_uses_by_alias(self):
        class Q(pydantic.BaseModel):
            search: str = pydantic.Field(alias="q")
            page: int = 1

        result = encode_params(Q(q="hi", page=2))
        assert result == {"q": "hi", "page": 2}

    def test_pydantic_unset_defaults_dropped(self):
        class Q(pydantic.BaseModel):
            search: str
            page: int = 1

        result = encode_params(Q(search="x"))
        # `page` defaulted and was not set — exclude_unset drops it.
        assert result == {"search": "x"}

    def test_list_values_stringified(self):
        result = encode_params({"ids": [1, 2, 3], "flags": [True, False]})
        assert result == {"ids": ["1", "2", "3"], "flags": ["true", "false"]}

    def test_list_drops_none_elements(self):
        result = encode_params({"x": [1, None, 2]})
        assert result == {"x": ["1", "2"]}


# ---------------------------------------------------------------------------
# encode_headers / encode_cookies
# ---------------------------------------------------------------------------

class TestEncodeHeaders:
    def test_none(self):
        assert encode_headers(None) is None
        assert encode_cookies(None) is None

    def test_stringifies_all_values(self):
        result = encode_headers({"X-Count": 42, "X-Flag": True})
        assert result == {"X-Count": "42", "X-Flag": "true"}

    def test_drops_none(self):
        result = encode_headers({"X-A": "a", "X-B": None})
        assert result == {"X-A": "a"}

    def test_basemodel(self):
        class H(pydantic.BaseModel):
            authorization: str = pydantic.Field(alias="Authorization")

        result = encode_headers(H(Authorization="Bearer xyz"))
        assert result == {"Authorization": "Bearer xyz"}

    def test_cookies_is_alias_for_headers(self):
        payload = {"sid": "abc123", "n": 1}
        assert encode_cookies(payload) == encode_headers(payload)

    def test_non_dict_input_returns_none(self):
        # Lists / primitives can't become headers — defensive guard.
        assert encode_headers([1, 2, 3]) is None
        assert encode_cookies([1, 2, 3]) is None
