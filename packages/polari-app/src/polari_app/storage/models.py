from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Metadata of one stored object.

    ``size`` and ``etag`` are ``None`` when the server did not report them.
    ``etag`` is an opaque version token, kept exactly as the server sent it
    (including quotes, e.g. ``'"5f3a"'``). It is not a content hash; only
    compare it for equality or pass it back as ``if_match``.
    """

    key: str
    size: int | None
    etag: str | None


@dataclass(frozen=True, slots=True)
class ObjectData:
    """An object's content together with the metadata it was read with."""

    info: ObjectInfo
    data: bytes


@dataclass(frozen=True, slots=True)
class ObjectListing:
    """Result of ``list_objects``. Neither tuple has a guaranteed order.

    ``common_prefixes`` are the next levels below the listed prefix, each
    ending with ``/`` (e.g. ``"2026/p01/"``). They are only filled when
    listing with ``delimiter="/"``. A prefix can be listed here even if no
    object is below it anymore (see ``delete_object``).
    """

    objects: tuple[ObjectInfo, ...]
    common_prefixes: tuple[str, ...]
