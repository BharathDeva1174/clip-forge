"""Streaming multipart upload handling.

Starlette's own multipart parser spools the whole file to a temporary file
*before* the endpoint runs, and it has no limit on file part size. That means
an oversized upload would be written to disk in full before the application
could reject it.

This module parses the multipart body itself with the same library Starlette
uses (``python-multipart``), but drives it from the request stream so that:

* the byte limit is enforced *while* the body is being received;
* the file is streamed straight to a file inside the application storage root,
  in bounded chunks, without ever being held in memory or spooled twice;
* a partial file is removed as soon as anything goes wrong.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request
from python_multipart.multipart import MultipartParser, parse_options_header

from app.errors import ErrorCode
from app.storage import Storage, sanitise_filename

logger = logging.getLogger("clipforge.uploads")

#: Size of the chunks written to disk.
_CHUNK_SIZE = 1024 * 1024


class UploadError(Exception):
    """The upload could not be accepted."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ReceivedUpload:
    """A file that was streamed to disk and is ready to be validated."""

    filename: str
    declared_content_type: str
    extension: str
    size_bytes: int
    path: Path


def _boundary(content_type: str) -> bytes:
    """Extract the multipart boundary from a Content-Type header."""
    _, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if not boundary:
        raise UploadError(
            400, ErrorCode.INVALID_REQUEST.value, "The multipart boundary is missing."
        )
    return boundary


class _Collector:
    """Collects the callbacks of the multipart parser."""

    def __init__(
        self,
        *,
        storage: Storage,
        max_bytes: int,
        field_name: str,
        allowed_extensions: list[str],
    ) -> None:
        self.storage = storage
        self.max_bytes = max_bytes
        self.field_name = field_name
        self.allowed_extensions = allowed_extensions

        self.filename: str | None = None
        self.declared_content_type = ""
        self.extension = ""
        self.size_bytes = 0
        self.path: Path | None = None
        self.file_parts = 0
        self.other_fields = 0

        self._handle: Any = None
        self._pending: list[bytes] = []
        self._header_name = b""
        self._header_value = b""
        self._part_headers: list[tuple[bytes, bytes]] = []
        self._in_file_part = False

    # -- parser callbacks ---------------------------------------------------

    def on_part_begin(self) -> None:
        self._part_headers = []
        self._in_file_part = False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        self._part_headers.append((self._header_name.lower(), self._header_value))
        self._header_name = b""
        self._header_value = b""

    def on_headers_finished(self) -> None:
        disposition = b""
        content_type = b""
        for name, value in self._part_headers:
            if name == b"content-disposition":
                disposition = value
            elif name == b"content-type":
                content_type = value

        _, options = parse_options_header(disposition)
        field_name = options.get(b"name", b"").decode("latin-1")
        filename = options.get(b"filename")

        if filename is None:
            self.other_fields += 1
            if self.other_fields > 10:
                raise UploadError(
                    400,
                    ErrorCode.INVALID_REQUEST.value,
                    "Too many form fields in the upload.",
                )
            return

        self.file_parts += 1
        if self.file_parts > 1:
            raise UploadError(
                400,
                ErrorCode.INVALID_REQUEST.value,
                "Only one file can be uploaded at a time.",
            )
        if field_name != self.field_name:
            raise UploadError(
                400,
                ErrorCode.INVALID_REQUEST.value,
                f"The uploaded file must be sent in the '{self.field_name}' field.",
            )

        original_name = filename.decode("latin-1", "replace")
        self.filename = sanitise_filename(original_name)
        self.declared_content_type = content_type.decode("latin-1", "replace")

        extension = Path(self.filename).suffix.lower()
        if extension not in self.allowed_extensions:
            raise UploadError(
                415,
                ErrorCode.UNSUPPORTED_MEDIA_TYPE.value,
                "That file type is not supported. Accepted types: "
                + ", ".join(self.allowed_extensions),
            )
        self.extension = extension

        self.storage.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.storage.uploads_dir / self.storage.new_source_name(extension)
        self._handle = open(self.path, "wb")  # noqa: SIM115 - closed in `close()`

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._handle is None:
            return
        self._pending.append(data[start:end])

    def on_part_end(self) -> None:
        return None

    def on_end(self) -> None:
        return None

    # -- writing ------------------------------------------------------------

    async def flush(self) -> None:
        """Write the buffered bytes of the current chunk to disk."""
        if not self._pending or self._handle is None:
            self._pending.clear()
            return
        payload = b"".join(self._pending)
        self._pending.clear()
        self.size_bytes += len(payload)
        if self.size_bytes > self.max_bytes:
            raise UploadError(
                413,
                ErrorCode.PAYLOAD_TOO_LARGE.value,
                "The file is larger than the configured upload limit.",
            )
        self._handle.write(payload)

    def close(self) -> None:
        """Close the destination file."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def cleanup(self) -> None:
        """Close and remove a partial file."""
        self.close()
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None


async def receive_single_upload(
    request: Request,
    *,
    storage: Storage,
    max_bytes: int,
    field_name: str = "file",
    allowed_extensions: list[str] | None = None,
) -> ReceivedUpload:
    """Stream one file part of a multipart request to disk.

    The byte limit is enforced while the body is being received. A partial
    file is always removed before an error is raised.
    """
    extensions = allowed_extensions or []
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise UploadError(
            415,
            ErrorCode.UNSUPPORTED_MEDIA_TYPE.value,
            "The upload must be sent as multipart/form-data.",
        )

    collector = _Collector(
        storage=storage,
        max_bytes=max_bytes,
        field_name=field_name,
        allowed_extensions=extensions,
    )
    parser = MultipartParser(
        _boundary(content_type),
        {
            "on_part_begin": collector.on_part_begin,
            "on_part_data": collector.on_part_data,
            "on_part_end": collector.on_part_end,
            "on_header_field": collector.on_header_field,
            "on_header_value": collector.on_header_value,
            "on_header_end": collector.on_header_end,
            "on_headers_finished": collector.on_headers_finished,
            "on_end": collector.on_end,
        },
    )

    try:
        async for chunk in request.stream():
            if chunk:
                parser.write(chunk)
                await collector.flush()
        parser.finalize()
        await collector.flush()
    except UploadError:
        collector.cleanup()
        raise
    except Exception as exc:  # malformed multipart, client disconnect, ...
        collector.cleanup()
        logger.info("upload failed while streaming: %s", type(exc).__name__)
        raise UploadError(
            400,
            ErrorCode.INVALID_REQUEST.value,
            "The upload could not be read. It may have been interrupted.",
        ) from exc

    if collector.path is None or collector.filename is None:
        collector.cleanup()
        raise UploadError(
            400,
            ErrorCode.INVALID_REQUEST.value,
            "No file was uploaded. Send the video in the 'file' field.",
        )

    if collector.size_bytes == 0:
        collector.cleanup()
        raise UploadError(
            400, ErrorCode.EMPTY_FILE.value, "The uploaded file is empty."
        )

    collector.close()
    return ReceivedUpload(
        filename=collector.filename,
        declared_content_type=collector.declared_content_type,
        extension=collector.extension,
        size_bytes=collector.size_bytes,
        path=collector.path,
    )
