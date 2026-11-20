from collections.abc import Callable
from urllib.parse import quote, unquote

import httpx

# Synthetic values only: the host can never resolve (.test) and no request
# ever leaves the process (see conftest.py).
ROOT_URL = "https://dav.example.test/remote.php/dav/files/synthetic-user/polari root"
ENCODED_ROOT_URL = (
    "https://dav.example.test/remote.php/dav/files/synthetic-user/polari%20root/"
)
ROOT_PATH = "/remote.php/dav/files/synthetic-user/polari root/"
ENCODED_ROOT_PATH = "/remote.php/dav/files/synthetic-user/polari%20root/"
USERNAME = "synthetic-user"
PASSWORD = "synthetic-password-not-real"

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Records every request and answers with a configurable handler."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = lambda request: httpx.Response(500)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self.handler(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.requests]


def multistatus(*responses: str, prefix: str = "d") -> bytes:
    """Build a 207 body; ``responses`` are inner ``<response>`` fragments."""
    inner = "".join(responses)
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<{prefix}:multistatus xmlns:{prefix}="DAV:">{inner}</{prefix}:multistatus>'
    ).encode()


def dav_response(
    href: str,
    *,
    collection: bool = False,
    length: int | None = None,
    etag: str | None = None,
    prefix: str = "d",
) -> str:
    """One ``<response>`` like Nextcloud sends it (missing props -> 404 group)."""
    p = prefix
    found = [
        f"<{p}:resourcetype><{p}:collection/></{p}:resourcetype>"
        if collection
        else f"<{p}:resourcetype/>"
    ]
    missing = []
    if length is None:
        missing.append(f"<{p}:getcontentlength/>")
    else:
        found.append(f"<{p}:getcontentlength>{length}</{p}:getcontentlength>")
    if etag is None:
        missing.append(f"<{p}:getetag/>")
    else:
        found.append(f"<{p}:getetag>{etag}</{p}:getetag>")
    groups = (
        f"<{p}:propstat><{p}:prop>{''.join(found)}</{p}:prop>"
        f"<{p}:status>HTTP/1.1 200 OK</{p}:status></{p}:propstat>"
    )
    if missing:
        groups += (
            f"<{p}:propstat><{p}:prop>{''.join(missing)}</{p}:prop>"
            f"<{p}:status>HTTP/1.1 404 Not Found</{p}:status></{p}:propstat>"
        )
    return f"<{p}:response><{p}:href>{href}</{p}:href>{groups}</{p}:response>"


def href(path: str, *, collection: bool = False) -> str:
    """Encoded href below the synthetic root for a plain relative path."""
    encoded = "/".join(quote(level, safe="") for level in path.split("/"))
    tail = "/" if collection and path else ""
    return ENCODED_ROOT_PATH + encoded + tail


def respond_207(body: bytes) -> Handler:
    return lambda request: httpx.Response(207, content=body)


class FakeNextcloud:
    """A tiny in-memory WebDAV server. Status codes follow what Nextcloud
    34.0.6 returned in the local Docker integration run; it is still a model.
    """

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}
        self.collections: set[str] = {""}
        self._counter = 0

    def add_file(self, path: str, data: bytes) -> str:
        for level in _parents(path):
            self.collections.add(level)
        etag = self._next_etag()
        self.files[path] = (data, etag)
        return etag

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.raw_path.decode())
        if path != ROOT_PATH.rstrip("/") and not path.startswith(ROOT_PATH):
            return httpx.Response(403)
        rel = path[len(ROOT_PATH) :].strip("/") if path.startswith(ROOT_PATH) else ""
        handler = getattr(self, "_" + request.method.lower(), None)
        if handler is None:
            return httpx.Response(405)
        response: httpx.Response = handler(request, rel)
        return response

    def _propfind(self, request: httpx.Request, rel: str) -> httpx.Response:
        if not self._exists(rel):
            return httpx.Response(404)
        entries = [self._entry(rel)]
        if request.headers.get("Depth") == "1" and rel in self.collections:
            children = [
                p for p in self.collections | set(self.files) if p and _parent(p) == rel
            ]
            entries += [self._entry(child) for child in children]
        return httpx.Response(207, content=multistatus(*entries))

    def _get(self, request: httpx.Request, rel: str) -> httpx.Response:
        if rel in self.collections:  # observed: HTML page, no ETag header
            return httpx.Response(200, html="<html>folder</html>")
        if rel not in self.files:
            return httpx.Response(404)
        data, etag = self.files[rel]
        return httpx.Response(200, content=data, headers={"ETag": etag})

    def _put(self, request: httpx.Request, rel: str) -> httpx.Response:
        # Order and codes as observed on Nextcloud 34: preconditions first
        # (anything at the key, even a collection, fails If-None-Match), then
        # a missing parent or a parent that is a file gives 404.
        current = self.files.get(rel)
        if request.headers.get("If-None-Match") == "*" and self._exists(rel):
            return httpx.Response(412)
        if_match = request.headers.get("If-Match")
        if if_match is not None and (current is None or current[1] != if_match):
            return httpx.Response(412)
        if _parent(rel) not in self.collections:
            return httpx.Response(404)
        etag = self._next_etag()
        self.files[rel] = (request.content, etag)
        return httpx.Response(201 if current is None else 204, headers={"ETag": etag})

    def _mkcol(self, request: httpx.Request, rel: str) -> httpx.Response:
        if self._exists(rel):
            return httpx.Response(405)
        if _parent(rel) not in self.collections:
            return httpx.Response(409)
        self.collections.add(rel)
        return httpx.Response(201)

    def _delete(self, request: httpx.Request, rel: str) -> httpx.Response:
        if not self._exists(rel):
            return httpx.Response(404)
        if_match = request.headers.get("If-Match")
        if if_match is not None and self._etag(rel) != if_match:
            return httpx.Response(412)
        # Like WebDAV: deleting a collection deletes everything below it.
        below = rel + "/"
        self.files = {
            p: v for p, v in self.files.items() if p != rel and not p.startswith(below)
        }
        self.collections = {
            p for p in self.collections if p != rel and not p.startswith(below)
        }
        return httpx.Response(204)

    def _exists(self, rel: str) -> bool:
        return rel in self.files or rel in self.collections

    def _etag(self, rel: str) -> str:
        if rel in self.files:
            return self.files[rel][1]
        return f'"collection-{len(rel)}"'

    def _entry(self, rel: str) -> str:
        if rel in self.collections:
            return dav_response(
                href(rel, collection=True), collection=True, etag=self._etag(rel)
            )
        data, etag = self.files[rel]
        return dav_response(href(rel), length=len(data), etag=etag)

    def _next_etag(self) -> str:
        self._counter += 1
        return f'"etag-{self._counter}"'


def _parent(path: str) -> str:
    return path.rpartition("/")[0]


def _parents(path: str) -> list[str]:
    levels = path.split("/")[:-1]
    return ["/".join(levels[: i + 1]) for i in range(len(levels))]
