import unicodedata
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from polari_app.storage.errors import InvalidKeyError, InvalidResponseError


def validate_key(key: str) -> str:
    """Validate an object key and return its canonical (NFC) form.

    A key is non-empty, relative and made of non-empty levels separated by
    ``/``: ``"2026/p01/experiment.json"``. Rejected are leading or trailing
    ``/``, empty levels (``"a//b"``), ``.``/``..`` levels, backslashes and
    control characters. Nothing is silently fixed.
    """
    if not isinstance(key, str):
        raise InvalidKeyError(f"Key must be a string, got {type(key)!r}")
    if key == "":
        raise InvalidKeyError("Key must not be empty")
    if key.startswith("/") or key.endswith("/"):
        raise InvalidKeyError(f"Key must not start or end with '/': {key!r}")
    _check_levels(key)
    return unicodedata.normalize("NFC", key)


def validate_prefix(prefix: str) -> str:
    """Validate a listing prefix: ``""`` or whole levels ending with ``/``.

    ``"2026/p01/"`` is valid; ``"2026/p0"`` is not, because Nextcloud can only
    list whole levels, not arbitrary substrings.
    """
    if not isinstance(prefix, str):
        raise InvalidKeyError(f"Prefix must be a string, got {type(prefix)!r}")
    if prefix == "":
        return ""
    if not prefix.endswith("/"):
        raise InvalidKeyError(f"Prefix must be empty or end with '/': {prefix!r}")
    try:
        return validate_key(prefix[:-1]) + "/"
    except InvalidKeyError as exc:
        raise InvalidKeyError(f"Invalid prefix {prefix!r}: {exc}") from exc


def encode_key(key: str) -> str:
    """Percent-encode a canonical key (or prefix) for use in a URL path.

    Every level is encoded on its own (UTF-8, ``safe=""``), so ``%``, ``?``,
    ``#`` and spaces can never change the URL structure.
    """
    return "/".join(quote(level, safe="") for level in key.split("/"))


def parent_prefixes(key: str) -> list[str]:
    """All prefixes above ``key``, outermost first.

    ``"a/b/c.json"`` -> ``["a/", "a/b/"]``; ``"c.json"`` -> ``[]``.
    """
    levels = key.split("/")[:-1]
    return ["/".join(levels[: i + 1]) + "/" for i in range(len(levels))]


def encode_root_url(root_url: str) -> tuple[str, str]:
    """Turn a plain (not percent-encoded) root URL into its request form.

    Returns ``(encoded_url, root_path)``. ``encoded_url`` ends with ``/`` and
    has every path level percent-encoded; ``root_path`` is the plain URL
    path ending with ``/``, used to interpret hrefs in responses.

    ``"https://cloud.example.org/dav/files/u/my data"`` ->
    ``("https://cloud.example.org/dav/files/u/my%20data/",
    "/dav/files/u/my data/")``.

    Raises ``ValueError`` for non-http(s) URLs, missing host, embedded
    credentials, query, fragment or ``.``/``..`` levels.
    """
    parts = urlsplit(root_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("root_url must be an absolute http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("root_url must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("root_url must not contain '?' or '#'")

    levels = [level for level in parts.path.split("/") if level]
    if any(level in (".", "..") for level in levels):
        raise ValueError("root_url must not contain '.' or '..' levels")
    root_path = "/" + "".join(f"{level}/" for level in levels)
    encoded_path = "/" + "".join(f"{quote(level, safe='')}/" for level in levels)
    encoded_url = urlunsplit((parts.scheme, parts.netloc, encoded_path, "", ""))
    return encoded_url, unicodedata.normalize("NFC", root_path)


def href_to_key(href: str, root_path: str) -> str:
    """Convert a WebDAV ``<d:href>`` into a canonical key (``""`` for the root).

    ``href`` may be an absolute URL or path and is percent-encoded.
    ``root_path`` is the plain root path from :func:`encode_root_url`. Raises
    :class:`InvalidResponseError` if the href is outside the root or does not
    decode into a valid key. Only the URL path is compared, not the host.
    """
    raw_path = urlsplit(href.strip()).path
    try:
        decoded = unquote(raw_path, errors="strict")
    except UnicodeDecodeError as exc:
        raise InvalidResponseError(f"href is not valid UTF-8: {href!r}") from exc
    decoded = unicodedata.normalize("NFC", decoded)

    if decoded.rstrip("/") + "/" == root_path:
        return ""
    if not decoded.startswith(root_path):
        raise InvalidResponseError(f"href is outside the storage root: {href!r}")
    try:
        return validate_key(decoded[len(root_path) :].removesuffix("/"))
    except InvalidKeyError as exc:
        raise InvalidResponseError(f"href is not a valid key: {href!r}") from exc


def _check_levels(key: str) -> None:
    if "\\" in key:
        raise InvalidKeyError(f"Backslashes are not allowed: {key!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in key):
        raise InvalidKeyError(f"Control characters are not allowed: {key!r}")
    for level in key.split("/"):
        if level == "":
            raise InvalidKeyError(f"Empty level in {key!r}")
        if level in (".", ".."):
            raise InvalidKeyError(f"'.' and '..' are not allowed: {key!r}")
