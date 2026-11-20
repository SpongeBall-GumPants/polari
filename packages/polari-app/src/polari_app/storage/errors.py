class StorageError(Exception):
    """Base class of all storage errors."""


class InvalidKeyError(StorageError, ValueError):
    """An object key or prefix is unsafe or malformed. Raised before any request."""


class HTTPStatusError(StorageError):
    """The server answered with a status code the operation does not accept.

    ``key`` is the object key or prefix of the operation, never a full URL.
    """

    def __init__(
        self, method: str, key: str, status_code: int, detail: str = ""
    ) -> None:
        self.method = method
        self.key = key
        self.status_code = status_code
        message = f"{method} {key!r} failed with HTTP {status_code}"
        super().__init__(f"{message}: {detail}" if detail else message)


class AuthenticationError(HTTPStatusError):
    """HTTP 401: missing or wrong credentials."""


class PermissionDeniedError(HTTPStatusError):
    """HTTP 403: authenticated, but not allowed to do this."""


class NotFoundError(HTTPStatusError):
    """No object exists at the key (HTTP 404, or the key names a collection)."""


class ConflictError(HTTPStatusError):
    """HTTP 409: the key cannot be created, e.g. a parent level is an object."""


class PreconditionFailedError(HTTPStatusError):
    """HTTP 412: an ``if_match`` / ``if_none_match`` condition did not hold."""


class UnexpectedStatusError(HTTPStatusError):
    """Any other status code that the operation does not accept."""


class LockedError(UnexpectedStatusError):
    """HTTP 423: the server's file lock refused the request.

    Nextcloud answers this when another request holds a lock on the same
    object, e.g. during concurrent writes. The refused request was not
    applied. Nothing retries automatically: while a new version can keep its
    predecessor's ETag (same-second writes), retrying an ``if_match`` write
    can turn a refusal into a lost update.
    """


class InvalidResponseError(StorageError):
    """The response could not be interpreted (malformed or unexpected)."""


class ObjectTooLargeError(StorageError):
    """A downloaded object is larger than the allowed maximum."""


class StorageTransportError(StorageError):
    """No usable HTTP response was received (network level problem)."""


class StorageTimeoutError(StorageTransportError):
    """The request timed out.

    For ``put_object``/``delete_object`` this means the request was never
    sent; otherwise :class:`OutcomeUnknownError` is raised.
    """


class StorageConnectionError(StorageTransportError):
    """The connection failed or broke.

    For ``put_object``/``delete_object`` this means the request was never
    sent; otherwise :class:`OutcomeUnknownError` is raised.
    """


class OutcomeUnknownError(StorageTransportError):
    """A write request failed after it may have reached the server.

    The object may or may not have been written or deleted. Check with
    ``head_object`` before retrying; never assume that the write failed.
    """
