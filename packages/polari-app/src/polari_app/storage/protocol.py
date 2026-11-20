from typing import Literal, Protocol

from polari_app.storage.models import ObjectData, ObjectInfo, ObjectListing


class ObjectStorage(Protocol):
    """Asynchronous key/value object storage.

    Keys are flat strings such as ``"2026/p01/experiment.json"``; ``/`` only
    matters as a level separator when listing. Keys are plain text, never
    percent-encoded. All methods raise subclasses of
    :class:`polari_app.storage.errors.StorageError`.
    """

    async def list_objects(
        self, prefix: str = "", *, delimiter: Literal["/"] | None = "/"
    ) -> ObjectListing:
        """List objects whose key starts with ``prefix``.

        ``prefix`` must be ``""`` or whole levels ending with ``/``. With
        ``delimiter="/"`` only the direct level is listed and deeper levels
        are returned as ``common_prefixes``; with ``delimiter=None`` every
        object below ``prefix`` is returned. A prefix without objects gives
        an empty listing, not an error.
        """
        ...

    async def head_object(self, key: str) -> ObjectInfo:
        """Return the metadata of the object at ``key``."""
        ...

    async def get_object(self, key: str) -> ObjectData:
        """Return the content and metadata of the object at ``key``."""
        ...

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> ObjectInfo:
        """Write ``data`` to ``key``. Exactly one condition must be given.

        ``if_none_match=True`` only creates a new object; ``if_match=etag``
        only replaces the object if it still has that ETag. A failed
        condition raises ``PreconditionFailedError``.
        """
        ...

    async def delete_object(self, key: str, *, if_match: str | None = None) -> None:
        """Delete the object at ``key``, optionally only if it has ``if_match``."""
        ...
