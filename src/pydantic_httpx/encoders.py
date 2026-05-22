"""Lightweight helpers built on top of pydantic v2's native JSON serialization.

Pydantic v2 already converts every value supported by FastAPI's old
``jsonable_encoder`` (datetimes, UUID, Enum, Decimal, IPvN, Path, SecretStr, ...)
via ``BaseModel.model_dump(mode="json")`` / ``TypeAdapter.dump_python(mode="json")``,
so we no longer ship that monolithic encoder.
"""
from typing import Any

import pydantic


def to_jsonable(
        obj: Any,
        *,
        by_alias: bool = False,
        exclude_unset: bool = True,
        exclude_none: bool = False,
        exclude_defaults: bool = False,
) -> Any:
    """Recursively convert ``obj`` into JSON-serializable primitives.

    Delegates to ``BaseModel.model_dump(mode="json", ...)`` for any nested
    pydantic models — that already handles ``datetime``, ``UUID``, ``Enum``,
    ``Decimal``, ``IPvN``, ``Path``, ``SecretStr`` and friends.
    """
    if obj is None:
        return None

    if isinstance(obj, pydantic.BaseModel):
        return obj.model_dump(
            mode="json",
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_none=exclude_none,
            exclude_defaults=exclude_defaults,
        )

    if isinstance(obj, dict):
        return {
            k: to_jsonable(
                v,
                by_alias=by_alias,
                exclude_unset=exclude_unset,
                exclude_none=exclude_none,
                exclude_defaults=exclude_defaults,
            )
            for k, v in obj.items()
            if not (exclude_none and v is None)
        }

    if isinstance(obj, (list, tuple, set, frozenset)):
        return [
            to_jsonable(
                v,
                by_alias=by_alias,
                exclude_unset=exclude_unset,
                exclude_none=exclude_none,
                exclude_defaults=exclude_defaults,
            )
            for v in obj
        ]

    return obj


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def encode_params(obj: Any) -> dict[str, Any] | None:
    """Prepare ``params=`` payload for httpx.

    httpx accepts primitives and sequences of primitives in query params, but
    serializes booleans as ``"True"/"False"``. We pre-stringify booleans to the
    more conventional lowercase ``"true"/"false"`` and drop ``None`` values.
    """
    if obj is None:
        return None

    data = to_jsonable(obj, by_alias=True, exclude_unset=True, exclude_none=True)

    if not isinstance(data, dict):
        return None

    encoded: dict[str, Any] = {}

    for key, value in data.items():
        if value is None:
            continue

        if isinstance(value, bool):
            encoded[str(key)] = _stringify(value)
        elif isinstance(value, (list, tuple)):
            encoded[str(key)] = [_stringify(v) for v in value if v is not None]
        else:
            encoded[str(key)] = value

    return encoded


def encode_headers(obj: Any) -> dict[str, str] | None:
    """Prepare ``headers=`` payload for httpx (all values stringified)."""
    if obj is None:
        return None

    data = to_jsonable(obj, by_alias=True, exclude_unset=True, exclude_none=True)

    if not isinstance(data, dict):
        return None

    return {str(k): _stringify(v) for k, v in data.items() if v is not None}


def encode_cookies(obj: Any) -> dict[str, str] | None:
    """Prepare ``cookies=`` payload for httpx (semantics identical to headers)."""
    return encode_headers(obj)
