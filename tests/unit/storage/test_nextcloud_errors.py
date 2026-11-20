import base64
from collections.abc import Awaitable, Callable

import httpx
import pytest
from dav_helpers import (
    ENCODED_ROOT_URL,
    PASSWORD,
    ROOT_URL,
    USERNAME,
    FakeNextcloud,
    Recorder,
    dav_response,
    href,
    multistatus,
)
from polari_app.storage import (
    AuthenticationError,
    ConflictError,
    HTTPStatusError,
    LockedError,
    NextcloudStorage,
    NotFoundError,
    OutcomeUnknownError,
    PermissionDeniedError,
    PreconditionFailedError,
    StorageConnectionError,
    StorageError,
    StorageTimeoutError,
    StorageTransportError,
    UnexpectedStatusError,
    nextcloud,
)

pytestmark = pytest.mark.anyio

Operation = Callable[[NextcloudStorage], Awaitable[object]]

READ_OPERATIONS: dict[str, Operation] = {
    "list_objects": lambda s: s.list_objects("d/"),
    "head_object": lambda s: s.head_object("d/a.json"),
    "get_object": lambda s: s.get_object("d/a.json"),
}
WRITE_OPERATIONS: dict[str, Operation] = {
    "put_if_none_match": lambda s: s.put_object(
        "d/new.json", b"{}", if_none_match=True
    ),
    "put_if_match": lambda s: s.put_object("d/a.json", b"{}", if_match='"e"'),
}
ALL_OPERATIONS = {**READ_OPERATIONS, **WRITE_OPERATIONS}


@pytest.mark.parametrize("operation", ALL_OPERATIONS)
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (412, PreconditionFailedError),
        (423, LockedError),
        (500, UnexpectedStatusError),
        (503, UnexpectedStatusError),
        (302, UnexpectedStatusError),
    ],
)
async def test_status_codes_map_to_typed_errors(
    storage: NextcloudStorage,
    recorder: Recorder,
    operation: str,
    status: int,
    error_type: type[HTTPStatusError],
) -> None:
    recorder.handler = lambda request: httpx.Response(status)
    with pytest.raises(error_type) as info:
        await ALL_OPERATIONS[operation](storage)
    assert info.value.status_code == status
    assert isinstance(info.value, StorageError)


@pytest.mark.parametrize("operation", ALL_OPERATIONS)
async def test_423_is_a_locked_error_and_never_retried(
    storage: NextcloudStorage, recorder: Recorder, operation: str
) -> None:
    # Regression for B3: typed, still an UnexpectedStatusError for existing
    # callers, and not retried (retries can cause lost updates, see B1).
    recorder.handler = lambda request: httpx.Response(423)
    with pytest.raises(LockedError) as info:
        await ALL_OPERATIONS[operation](storage)
    assert isinstance(info.value, UnexpectedStatusError)
    assert info.value.status_code == 423
    assert len(recorder.requests) == 1


async def test_delete_423_is_a_locked_error(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    fake.add_file("a.json", b"1")
    recorder.handler = lambda request: (
        fake(request) if request.method == "PROPFIND" else httpx.Response(423)
    )
    with pytest.raises(LockedError):
        await storage.delete_object("a.json")
    assert recorder.methods == ["PROPFIND", "DELETE"]
    assert "a.json" in fake.files


@pytest.mark.parametrize("operation", ["head_object", "get_object"])
async def test_404_is_not_found_for_object_reads(
    storage: NextcloudStorage, recorder: Recorder, operation: str
) -> None:
    recorder.handler = lambda request: httpx.Response(404)
    with pytest.raises(NotFoundError):
        await ALL_OPERATIONS[operation](storage)


async def test_409_is_conflict_for_if_match_put(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = lambda request: httpx.Response(409)
    with pytest.raises(ConflictError):
        await WRITE_OPERATIONS["put_if_match"](storage)


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(403, PermissionDeniedError), (507, UnexpectedStatusError)],
)
async def test_mkcol_failures_are_typed(
    storage: NextcloudStorage,
    recorder: Recorder,
    status: int,
    error_type: type[HTTPStatusError],
) -> None:
    recorder.handler = lambda request: httpx.Response(
        409 if request.method == "PUT" else status
    )
    with pytest.raises(error_type) as info:
        await storage.put_object("a/b.json", b"{}", if_none_match=True)
    assert info.value.method == "MKCOL"
    assert info.value.key == "a/"


@pytest.mark.parametrize("status", [401, 403, 412, 500])
async def test_delete_status_codes(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder, status: int
) -> None:
    fake.add_file("a.json", b"1")
    recorder.handler = lambda request: (
        fake(request) if request.method == "PROPFIND" else httpx.Response(status)
    )
    with pytest.raises(HTTPStatusError) as info:
        await storage.delete_object("a.json")
    assert info.value.status_code == status
    assert info.value.method == "DELETE"


@pytest.mark.parametrize("operation", ALL_OPERATIONS)
async def test_redirects_are_not_followed(
    storage: NextcloudStorage, recorder: Recorder, operation: str
) -> None:
    recorder.handler = lambda request: httpx.Response(
        301, headers={"Location": "https://elsewhere.example.test/"}
    )
    with pytest.raises(UnexpectedStatusError) as info:
        await ALL_OPERATIONS[operation](storage)
    assert info.value.status_code == 301
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("operation", READ_OPERATIONS)
@pytest.mark.parametrize(
    "exception_type", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout]
)
async def test_read_timeouts(
    storage: NextcloudStorage,
    recorder: Recorder,
    operation: str,
    exception_type: type[httpx.TimeoutException],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception_type("synthetic timeout", request=request)

    recorder.handler = handler
    with pytest.raises(StorageTimeoutError) as info:
        await READ_OPERATIONS[operation](storage)
    assert isinstance(info.value.__cause__, exception_type)


@pytest.mark.parametrize("operation", READ_OPERATIONS)
@pytest.mark.parametrize(
    "exception_type", [httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError]
)
async def test_read_connection_failures(
    storage: NextcloudStorage,
    recorder: Recorder,
    operation: str,
    exception_type: type[httpx.TransportError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception_type("synthetic failure", request=request)

    recorder.handler = handler
    with pytest.raises(StorageConnectionError):
        await READ_OPERATIONS[operation](storage)


NOT_SENT_OR_UNKNOWN = [
    (httpx.ConnectError, StorageConnectionError),
    (httpx.ConnectTimeout, StorageTimeoutError),
    (httpx.PoolTimeout, StorageTimeoutError),
    (httpx.WriteTimeout, OutcomeUnknownError),
    (httpx.ReadTimeout, OutcomeUnknownError),
    (httpx.ReadError, OutcomeUnknownError),
    (httpx.WriteError, OutcomeUnknownError),
    (httpx.RemoteProtocolError, OutcomeUnknownError),
]


@pytest.mark.parametrize("operation", WRITE_OPERATIONS)
@pytest.mark.parametrize(("exception_type", "error_type"), NOT_SENT_OR_UNKNOWN)
async def test_put_distinguishes_not_sent_from_unknown_outcome(
    storage: NextcloudStorage,
    recorder: Recorder,
    operation: str,
    exception_type: type[httpx.TransportError],
    error_type: type[StorageTransportError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception_type("synthetic failure", request=request)

    recorder.handler = handler
    with pytest.raises(error_type) as info:
        await WRITE_OPERATIONS[operation](storage)
    assert type(info.value) is error_type
    assert len(recorder.requests) == 1  # never retried automatically


@pytest.mark.parametrize(("exception_type", "error_type"), NOT_SENT_OR_UNKNOWN)
async def test_delete_distinguishes_not_sent_from_unknown_outcome(
    storage: NextcloudStorage,
    fake: FakeNextcloud,
    recorder: Recorder,
    exception_type: type[httpx.TransportError],
    error_type: type[StorageTransportError],
) -> None:
    fake.add_file("a.json", b"1")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return fake(request)
        raise exception_type("synthetic failure", request=request)

    recorder.handler = handler
    with pytest.raises(error_type) as info:
        await storage.delete_object("a.json")
    assert type(info.value) is error_type
    assert recorder.methods == ["PROPFIND", "DELETE"]


async def test_mkcol_transport_failure_is_not_outcome_unknown(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "MKCOL":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(409)

    recorder.handler = handler
    # The object PUT was refused (409), so the object was definitely not written.
    with pytest.raises(StorageTimeoutError):
        await storage.put_object("a/b.json", b"{}", if_none_match=True)


@pytest.mark.parametrize("operation", ALL_OPERATIONS)
async def test_timeout_is_applied_to_every_request(
    storage: NextcloudStorage, recorder: Recorder, operation: str
) -> None:
    recorder.handler = lambda request: httpx.Response(500)
    with pytest.raises(UnexpectedStatusError):
        await ALL_OPERATIONS[operation](storage)
    assert recorder.last.extensions["timeout"] == nextcloud._TIMEOUT.as_dict()


async def test_basic_auth_is_sent(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = lambda request: httpx.Response(
        207, content=multistatus(dav_response(href("a.json"), etag='"e"'))
    )
    await storage.head_object("a.json")
    token = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    assert recorder.last.headers["Authorization"] == f"Basic {token}"


async def test_errors_do_not_leak_credentials_or_server_url(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = lambda request: httpx.Response(401, text="Unauthorized echo")
    with pytest.raises(AuthenticationError) as info:
        await storage.get_object("a.json")

    message = str(info.value)
    assert message == "GET 'a.json' failed with HTTP 401"
    for secret in (PASSWORD, USERNAME, "dav.example.test", "Basic"):
        assert secret not in message
        assert secret not in repr(info.value)


async def test_root_url_is_encoded_in_constructor(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = lambda request: httpx.Response(404)
    await storage.list_objects()
    assert str(recorder.last.url) == ENCODED_ROOT_URL
    assert recorder.last.url.raw_path.endswith(b"/polari%20root/")


@pytest.mark.parametrize(
    "root_url", ["ftp://x.example.test/", "https://someone:secret@x.example.test/"]
)
def test_constructor_rejects_bad_root_url(root_url: str) -> None:
    with pytest.raises(ValueError, match="root_url must"):
        NextcloudStorage(root_url, USERNAME, PASSWORD)


async def test_context_manager_closes_client(recorder: Recorder) -> None:
    async with NextcloudStorage(ROOT_URL, USERNAME, PASSWORD) as storage:
        pass
    with pytest.raises(RuntimeError):
        await storage.head_object("a.json")


async def test_aclose_closes_client(recorder: Recorder) -> None:
    storage = NextcloudStorage(ROOT_URL, USERNAME, PASSWORD)
    await storage.aclose()
    with pytest.raises(RuntimeError):
        await storage.get_object("a.json")
