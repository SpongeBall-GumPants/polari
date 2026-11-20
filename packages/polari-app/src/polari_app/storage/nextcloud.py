import asyncio
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Self

import httpx

from polari_app.storage.errors import (
    AuthenticationError,
    ConflictError,
    HTTPStatusError,
    InvalidResponseError,
    LockedError,
    NotFoundError,
    ObjectTooLargeError,
    OutcomeUnknownError,
    PermissionDeniedError,
    PreconditionFailedError,
    StorageConnectionError,
    StorageError,
    StorageTimeoutError,
    StorageTransportError,
    UnexpectedStatusError,
)
from polari_app.storage.keys import (
    encode_key,
    encode_root_url,
    href_to_key,
    parent_prefixes,
    validate_key,
    validate_prefix,
)
from polari_app.storage.models import ObjectData, ObjectInfo, ObjectListing
from polari_app.storage.propfind import PROPFIND_BODY, parse_multistatus

# Seconds per phase; meant for small JSON/YAML objects, not large XDF data.
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
_MAX_OBJECT_BYTES = 16 * 1024 * 1024
# A collection created by a concurrent MKCOL can be briefly invisible to PUT
# (observed on Nextcloud 34: MKCOL says 405 "exists", PUT still says 404).
_PARENT_ATTEMPTS = 3
_PARENT_RETRY_DELAY = 0.1

_STATUS_ERRORS: Mapping[int, type[HTTPStatusError]] = {
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    412: PreconditionFailedError,
    423: LockedError,
}

# Failures that happen before the request could reach the server.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


@dataclass(frozen=True, slots=True)
class _Resource:
    key: str
    is_collection: bool
    size: int | None
    etag: str | None


class NextcloudStorage:
    """``ObjectStorage`` backed by a folder on a Nextcloud server (WebDAV).

    ``root_url`` is the plain, not percent-encoded URL of the folder that
    holds all objects, e.g.
    ``"https://cloud.example.org/remote.php/dav/files/example-user/polari data"``.
    ``username`` and ``password`` (preferably a Nextcloud app password) are
    sent with HTTP Basic auth.

    A key ``"a/b/c.json"`` is stored as the file ``a/b/c.json`` below the
    root. The collections ``a/`` and ``a/b/`` are created when an object is
    first written below them and only appear as ``common_prefixes``.

    The storage owns its HTTP connection pool: use it as an async context
    manager or call :meth:`aclose`.
    """

    def __init__(self, root_url: str, username: str, password: str) -> None:
        self._root_url, self._root_path = encode_root_url(root_url)
        self._client = httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password),
            timeout=_TIMEOUT,
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        await self._client.aclose()

    async def list_objects(
        self, prefix: str = "", *, delimiter: Literal["/"] | None = "/"
    ) -> ObjectListing:
        """List objects below ``prefix``; see :meth:`ObjectStorage.list_objects`.

        ``delimiter="/"`` is a single ``PROPFIND Depth: 1``. ``delimiter=None``
        walks the collections one level at a time (Nextcloud refuses
        ``Depth: infinity``), so it needs several requests and is not a
        consistent snapshot if objects change meanwhile.
        """
        prefix = validate_prefix(prefix)
        if delimiter not in ("/", None):
            raise ValueError("delimiter must be '/' or None")

        objects, prefixes = await self._list_level(prefix)
        if delimiter == "/":
            return ObjectListing(tuple(objects), tuple(prefixes))

        pending = list(prefixes)
        while pending:
            level_objects, level_prefixes = await self._list_level(pending.pop())
            objects.extend(level_objects)
            pending.extend(level_prefixes)
        return ObjectListing(tuple(objects), ())

    async def head_object(self, key: str) -> ObjectInfo:
        """``PROPFIND Depth: 0``; collections are not objects (``NotFoundError``)."""
        key = validate_key(key)
        resource = await self._stat("PROPFIND", key)
        return ObjectInfo(key, resource.size, resource.etag)

    async def get_object(self, key: str) -> ObjectData:
        """``GET`` the object; refuses objects larger than 16 MiB.

        The returned ``etag`` comes from the response ``ETag`` header, so it
        belongs to exactly the bytes that were read.

        Nextcloud answers ``GET`` on a collection with ``200`` and an HTML
        page but no ``ETag``. So a response without ``ETag`` is checked with
        ``PROPFIND Depth: 0``; a collection raises ``NotFoundError``.
        """
        key = validate_key(key)
        try:
            # Without compression the ETag is exactly the stored one; Apache's
            # mod_deflate would otherwise append "-gzip" and break If-Match.
            async with self._client.stream(
                "GET", self._url(key), headers={"Accept-Encoding": "identity"}
            ) as response:
                _check_status("GET", key, response, accepted={200})
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > _MAX_OBJECT_BYTES:
                    raise _too_large(key)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > _MAX_OBJECT_BYTES:
                        raise _too_large(key)
                etag = response.headers.get("ETag")
        except httpx.RequestError as exc:
            raise _request_error("GET", key, exc, write=False) from exc
        if etag is None:
            await self._stat("GET", key)  # raises NotFoundError for a collection
        return ObjectData(ObjectInfo(key, len(data), etag), bytes(data))

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> ObjectInfo:
        """Conditional ``PUT``. There is deliberately no unconditional write.

        ``if_none_match=True`` sends ``If-None-Match: *`` and accepts only
        ``201 Created``. If a parent collection is missing (Nextcloud answers
        404; other servers 409), all parents are created with ``MKCOL`` and
        the ``PUT`` is retried (at most 3 rounds, because a collection made by
        a concurrent writer can be briefly invisible). If it still fails that
        way, a parent level is an object and ``ConflictError`` is raised.
        Anything already at ``key``, including a collection, gives
        ``PreconditionFailedError``.

        ``if_match=etag`` sends ``If-Match`` and accepts only ``200``/``204``
        (replaced). Parents are never created for it: the object must exist.

        The returned ``size`` is the number of bytes sent and ``etag`` the
        response ``ETag`` header, which may be ``None``.
        """
        key = validate_key(key)
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes; encode text first (UTF-8)")
        if (if_match is not None) == if_none_match:
            raise ValueError(
                "Pass exactly one of if_match=<etag> or if_none_match=True"
            )
        if if_match == "":
            raise ValueError("if_match must be a non-empty ETag")

        headers = {"Content-Type": "application/octet-stream"}
        if if_match is not None:
            headers["If-Match"] = if_match
        else:
            headers["If-None-Match"] = "*"

        response = await self._send("PUT", key, headers=headers, content=data)
        if if_none_match and parent_prefixes(key):
            attempt = 0
            while response.status_code in (404, 409):
                if attempt == _PARENT_ATTEMPTS:
                    raise ConflictError(
                        "PUT", key, response.status_code, "a parent level is an object"
                    )
                if attempt:
                    await asyncio.sleep(_PARENT_RETRY_DELAY * attempt)
                attempt += 1
                await self._create_parents(key)
                response = await self._send("PUT", key, headers=headers, content=data)
        if if_none_match and response.status_code in (200, 204):
            # Observed on Nextcloud 34 under concurrent creates: the server
            # replaced an object created a moment earlier by another writer.
            raise UnexpectedStatusError(
                "PUT",
                key,
                response.status_code,
                "server replaced an existing object despite If-None-Match: *",
            )
        _check_status(
            "PUT", key, response, accepted={201} if if_none_match else {200, 204}
        )
        return ObjectInfo(key, len(data), response.headers.get("ETag"))

    async def delete_object(self, key: str, *, if_match: str | None = None) -> None:
        """``DELETE`` one object, always guarded by ``If-Match``.

        ``PROPFIND Depth: 0`` first makes sure ``key`` is an object and not a
        collection (deleting a collection would delete everything below it).
        Without ``if_match`` the ETag seen there is used, so the ``DELETE``
        fails (``PreconditionFailedError``) if the object changed in between.

        Empty parent collections are left in place and may still appear as
        ``common_prefixes``.
        """
        key = validate_key(key)
        if if_match == "":
            raise ValueError("if_match must be a non-empty ETag")
        resource = await self._stat("DELETE", key)
        expected = if_match if if_match is not None else resource.etag
        if expected is None:
            raise InvalidResponseError(
                f"DELETE {key!r}: server reported no ETag; refusing unguarded delete"
            )
        response = await self._send("DELETE", key, headers={"If-Match": expected})
        _check_status("DELETE", key, response, accepted={200, 204})

    def _url(self, key_or_prefix: str) -> str:
        return (
            self._root_url + encode_key(key_or_prefix)
            if key_or_prefix
            else self._root_url
        )

    async def _list_level(self, prefix: str) -> tuple[list[ObjectInfo], list[str]]:
        try:
            resources = await self._propfind(prefix, depth="1")
        except NotFoundError:
            return [], []

        level = prefix.removesuffix("/")
        own: _Resource | None = None
        objects: list[ObjectInfo] = []
        prefixes: list[str] = []
        for resource in resources:
            if resource.key == level:
                if own is not None:
                    raise InvalidResponseError(f"PROPFIND {prefix!r}: duplicate self")
                own = resource
            elif level != "" and resource.key == "":
                raise InvalidResponseError(
                    f"PROPFIND {prefix!r}: unexpected root entry"
                )
            elif resource.key.rpartition("/")[0] == level:
                if resource.is_collection:
                    prefixes.append(resource.key + "/")
                else:
                    objects.append(
                        ObjectInfo(resource.key, resource.size, resource.etag)
                    )
            else:
                raise InvalidResponseError(
                    f"PROPFIND {prefix!r}: {resource.key!r} is not a direct child"
                )
        if own is None:
            raise InvalidResponseError(f"PROPFIND {prefix!r}: listed level missing")
        if not own.is_collection:
            return [], []  # the prefix names an object, so nothing is below it
        return objects, prefixes

    async def _stat(self, method: str, key: str) -> _Resource:
        resources = await self._propfind(key, depth="0")
        if len(resources) != 1 or resources[0].key != key:
            raise InvalidResponseError(f"PROPFIND {key!r}: expected exactly one entry")
        if resources[0].is_collection:
            raise NotFoundError(method, key, 207, "key is a prefix, not an object")
        return resources[0]

    async def _propfind(self, key_or_prefix: str, *, depth: str) -> list[_Resource]:
        response = await self._send(
            "PROPFIND",
            key_or_prefix,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            content=PROPFIND_BODY,
        )
        _check_status("PROPFIND", key_or_prefix, response, accepted={207})
        try:
            return [
                _Resource(
                    href_to_key(item.href, self._root_path),
                    item.is_collection,
                    item.content_length,
                    item.etag,
                )
                for item in parse_multistatus(response.content)
            ]
        except InvalidResponseError as exc:
            raise InvalidResponseError(f"PROPFIND {key_or_prefix!r}: {exc}") from exc

    async def _create_parents(self, key: str) -> None:
        for prefix in parent_prefixes(key):
            response = await self._send("MKCOL", prefix)
            # 405: something already exists there. If it is an object rather
            # than a collection, the retried PUT fails (404/409).
            # 409/423: a concurrent writer is still creating this level or its
            # parent (observed on Nextcloud 34). The bounded PUT retry in
            # put_object decides; a lasting problem ends in ConflictError.
            _check_status("MKCOL", prefix, response, accepted={201, 405, 409, 423})

    async def _send(
        self,
        method: str,
        key_or_prefix: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method, self._url(key_or_prefix), headers=headers, content=content
            )
        except httpx.RequestError as exc:
            write = method in ("PUT", "DELETE")
            raise _request_error(method, key_or_prefix, exc, write=write) from exc


def _check_status(
    method: str, key: str, response: httpx.Response, *, accepted: Collection[int]
) -> None:
    if response.status_code in accepted:
        return
    error_type = _STATUS_ERRORS.get(response.status_code, UnexpectedStatusError)
    raise error_type(method, key, response.status_code)


def _too_large(key: str) -> ObjectTooLargeError:
    return ObjectTooLargeError(
        f"GET {key!r}: object exceeds the limit of {_MAX_OBJECT_BYTES} bytes"
    )


def _request_error(
    method: str, key: str, exc: httpx.RequestError, *, write: bool
) -> StorageError:
    # Only the exception class name goes into the message; the original
    # exception stays available as ``__cause__``.
    kind = type(exc).__name__
    if write and not isinstance(exc, _NOT_SENT):
        return OutcomeUnknownError(
            f"{method} {key!r}: {kind}; the request may or may not have taken "
            "effect, check with head_object before retrying"
        )
    if isinstance(exc, httpx.TimeoutException):
        return StorageTimeoutError(f"{method} {key!r} timed out ({kind})")
    if isinstance(exc, httpx.DecodingError):
        return InvalidResponseError(f"{method} {key!r}: undecodable body ({kind})")
    if isinstance(exc, httpx.TransportError):
        return StorageConnectionError(f"{method} {key!r} failed ({kind})")
    return StorageTransportError(f"{method} {key!r} failed ({kind})")
