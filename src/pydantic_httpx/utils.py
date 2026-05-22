from collections.abc import AsyncIterator
from os import PathLike

import aiofiles

DEFAULT_DOWNLOAD_CHUNK_SIZE = 64 * 1024
DEFAULT_UPLOAD_CHUNK_SIZE = 64 * 1024

PathLikeT = str | PathLike[str]


async def read_file_by_chunk(
        file: PathLikeT,
        chunk_size: int = DEFAULT_UPLOAD_CHUNK_SIZE,
) -> AsyncIterator[bytes]:
    async with aiofiles.open(file, 'rb') as f:
        while chunk := await f.read(chunk_size):
            yield chunk
