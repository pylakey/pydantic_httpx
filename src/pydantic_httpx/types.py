from typing import Any

import pydantic

StrIntMapping = dict[str, str | int]
HttpEncodableMapping = dict[str, str | int | list[str | int]]
Params = HttpEncodableMapping | pydantic.BaseModel
Cookies = StrIntMapping | pydantic.BaseModel
Headers = StrIntMapping | pydantic.BaseModel
Body = dict[str, Any] | pydantic.BaseModel
ErrorResponseModels = dict[int, type[pydantic.BaseModel]]


class EmptyResponse(pydantic.BaseModel):
    pass
