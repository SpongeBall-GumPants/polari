"""Asynchronous object storage with a Nextcloud implementation.

Objects are addressed by keys such as ``"2026/p01/experiment.json"``; ``/``
separates levels when listing. Writes are always conditional, using the
object's ETag as an opaque version token.

Typical use::

    async with NextcloudStorage(root_url, username, app_password) as storage:
        key = "2026/p01/experiment.json"
        await storage.put_object(key, data, if_none_match=True)

        listing = await storage.list_objects("2026/")
        # listing.common_prefixes == ("2026/p01/",)

        current = await storage.get_object(key)
        await storage.put_object(key, new_data, if_match=current.info.etag)
"""

from polari_app.storage.errors import (
    AuthenticationError,
    ConflictError,
    HTTPStatusError,
    InvalidKeyError,
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
from polari_app.storage.models import ObjectData, ObjectInfo, ObjectListing
from polari_app.storage.nextcloud import NextcloudStorage
from polari_app.storage.protocol import ObjectStorage

__all__ = [
    "AuthenticationError",
    "ConflictError",
    "HTTPStatusError",
    "InvalidKeyError",
    "InvalidResponseError",
    "LockedError",
    "NextcloudStorage",
    "NotFoundError",
    "ObjectData",
    "ObjectInfo",
    "ObjectListing",
    "ObjectStorage",
    "ObjectTooLargeError",
    "OutcomeUnknownError",
    "PermissionDeniedError",
    "PreconditionFailedError",
    "StorageConnectionError",
    "StorageError",
    "StorageTimeoutError",
    "StorageTransportError",
    "UnexpectedStatusError",
]
