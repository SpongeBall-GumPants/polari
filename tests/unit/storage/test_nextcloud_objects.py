from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from dav_helpers import (
    ENCODED_ROOT_URL,
    FakeNextcloud,
    Recorder,
    dav_response,
    href,
    multistatus,
    respond_207,
)
from polari_app.storage import (
    ConflictError,
    InvalidKeyError,
    InvalidResponseError,
    NextcloudStorage,
    NotFoundError,
    ObjectInfo,
    ObjectTooLargeError,
    PreconditionFailedError,
    UnexpectedStatusError,
    nextcloud,
)

pytestmark = pytest.mark.anyio

JSON_BYTES = '{"title": "Synthetic test", "pilot": "p01", "note": "ü"}\n'.encode()


async def test_head_object_uses_depth_0(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = respond_207(
        multistatus(dav_response(href("a b.json"), length=5, etag='"e"'))
    )
    assert await storage.head_object("a b.json") == ObjectInfo("a b.json", 5, '"e"')
    assert recorder.last.method == "PROPFIND"
    assert recorder.last.headers["Depth"] == "0"
    assert str(recorder.last.url) == ENCODED_ROOT_URL + "a%20b.json"


async def test_head_object_missing_optional_props(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = respond_207(multistatus(dav_response(href("a.json"))))
    assert await storage.head_object("a.json") == ObjectInfo("a.json", None, None)


async def test_head_object_missing(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    with pytest.raises(NotFoundError) as info:
        await storage.head_object("nope.json")
    assert info.value.status_code == 404


async def test_head_object_hides_collections(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    fake.add_file("2026/p01/a.json", b"1")
    with pytest.raises(NotFoundError, match="prefix, not an object"):
        await storage.head_object("2026/p01")


@pytest.mark.parametrize(
    "body",
    [
        multistatus(),
        multistatus(dav_response(href("other.json"))),
        multistatus(dav_response(href("a.json")), dav_response(href("a.json"))),
    ],
)
async def test_head_object_requires_exactly_the_requested_entry(
    storage: NextcloudStorage, recorder: Recorder, body: bytes
) -> None:
    recorder.handler = respond_207(body)
    with pytest.raises(InvalidResponseError):
        await storage.head_object("a.json")


async def test_get_object_returns_exact_bytes_and_etag(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    payload = JSON_BYTES + b"\x00\xff\r\n"  # not even valid UTF-8: bytes stay bytes
    recorder.handler = lambda request: httpx.Response(
        200, content=payload, headers={"ETag": '"g1"'}
    )
    result = await storage.get_object("2026/p01/experiment.json")

    assert result.data == payload
    assert result.info == ObjectInfo("2026/p01/experiment.json", len(payload), '"g1"')
    assert recorder.last.method == "GET"
    assert str(recorder.last.url) == ENCODED_ROOT_URL + "2026/p01/experiment.json"


async def test_get_object_without_etag_checks_it_is_not_a_collection(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    file_entry = multistatus(dav_response(href("empty.json"), length=0))
    recorder.handler = lambda request: (
        httpx.Response(207, content=file_entry)
        if request.method == "PROPFIND"
        else httpx.Response(200, content=b"")
    )
    result = await storage.get_object("empty.json")
    assert result.data == b""
    assert result.info.etag is None
    assert recorder.methods == ["GET", "PROPFIND"]


async def test_get_object_on_a_collection_is_not_found(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    # Regression: Nextcloud 34 answers GET on a folder with 200 + HTML page.
    fake.add_file("level/inner.json", b"{}")
    with pytest.raises(NotFoundError, match="prefix, not an object"):
        await storage.get_object("level")
    assert recorder.methods == ["GET", "PROPFIND"]


async def test_get_object_with_etag_needs_no_extra_request(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    fake.add_file("a.json", b"{}")
    await storage.get_object("a.json")
    assert recorder.methods == ["GET"]


async def test_get_object_missing(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    with pytest.raises(NotFoundError) as info:
        await storage.get_object("missing.json")
    assert info.value.method == "GET"


async def test_get_object_rejects_declared_oversize(
    storage: NextcloudStorage, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nextcloud, "_MAX_OBJECT_BYTES", 10)
    recorder.handler = lambda request: httpx.Response(
        200, headers={"Content-Length": "11"}, content=b"x" * 11
    )
    with pytest.raises(ObjectTooLargeError):
        await storage.get_object("big.json")


async def test_get_object_rejects_streamed_oversize(
    storage: NextcloudStorage, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"x" * 4

    monkeypatch.setattr(nextcloud, "_MAX_OBJECT_BYTES", 10)
    recorder.handler = lambda request: httpx.Response(200, content=chunks())
    with pytest.raises(ObjectTooLargeError):
        await storage.get_object("big.json")


async def test_get_object_exactly_at_limit(
    storage: NextcloudStorage, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nextcloud, "_MAX_OBJECT_BYTES", 10)
    recorder.handler = lambda request: httpx.Response(
        200, content=b"x" * 10, headers={"ETag": '"e"'}
    )
    assert (await storage.get_object("ok.json")).data == b"x" * 10


async def test_put_if_none_match_sends_conditional_put(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = lambda request: httpx.Response(201, headers={"ETag": '"n1"'})
    info = await storage.put_object(
        "p01/new experiment.json", JSON_BYTES, if_none_match=True
    )

    request = recorder.last
    assert request.method == "PUT"
    assert request.headers["If-None-Match"] == "*"
    assert "If-Match" not in request.headers
    assert request.headers["Content-Type"] == "application/octet-stream"
    assert request.content == JSON_BYTES
    assert str(request.url) == ENCODED_ROOT_URL + "p01/new%20experiment.json"
    assert info == ObjectInfo("p01/new experiment.json", len(JSON_BYTES), '"n1"')


async def test_put_if_none_match_does_not_overwrite(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    fake.add_file("a.json", b"original")
    with pytest.raises(PreconditionFailedError) as info:
        await storage.put_object("a.json", b"replacement", if_none_match=True)

    assert info.value.status_code == 412
    assert fake.files["a.json"][0] == b"original"
    assert recorder.methods == ["PUT"]  # no retry, no unconditional fallback


async def test_put_if_match_replaces_current_version(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    etag = fake.add_file("a.json", b"v1")
    info = await storage.put_object("a.json", b"v2", if_match=etag)

    assert recorder.last.headers["If-Match"] == etag
    assert "If-None-Match" not in recorder.last.headers
    assert fake.files["a.json"][0] == b"v2"
    assert info.etag == fake.files["a.json"][1]
    assert info.etag != etag


async def test_put_if_match_with_stale_etag_fails(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    fake.add_file("a.json", b"v1")
    with pytest.raises(PreconditionFailedError):
        await storage.put_object("a.json", b"v2", if_match='"stale"')
    assert fake.files["a.json"][0] == b"v1"


async def test_put_if_match_on_missing_object_fails(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    with pytest.raises(PreconditionFailedError):
        await storage.put_object("a.json", b"v2", if_match='"any"')
    assert "a.json" not in fake.files


async def test_put_creates_missing_parent_collections(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    info = await storage.put_object(
        "2026/p01/trial 1/a.json", b"{}", if_none_match=True
    )

    assert recorder.methods == ["PUT", "MKCOL", "MKCOL", "MKCOL", "PUT"]
    assert [str(r.url) for r in recorder.requests[1:4]] == [
        ENCODED_ROOT_URL + "2026/",
        ENCODED_ROOT_URL + "2026/p01/",
        ENCODED_ROOT_URL + "2026/p01/trial%201/",
    ]
    assert fake.files["2026/p01/trial 1/a.json"][0] == b"{}"
    assert info.key == "2026/p01/trial 1/a.json"
    # Intermediate collections are only visible as prefixes, not as objects.
    listing = await storage.list_objects("2026/p01/")
    assert listing.objects == ()
    assert listing.common_prefixes == ("2026/p01/trial 1/",)


async def test_put_tolerates_partially_existing_parents(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    fake.collections.add("2026")
    await storage.put_object("2026/p01/a.json", b"{}", if_none_match=True)
    assert recorder.methods == ["PUT", "MKCOL", "MKCOL", "PUT"]
    assert "2026/p01/a.json" in fake.files


async def test_put_does_not_create_parents_when_parent_exists(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    fake.collections.add("2026")
    await storage.put_object("2026/a.json", b"{}", if_none_match=True)
    assert recorder.methods == ["PUT"]


async def test_put_below_an_object_is_a_conflict(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    fake.add_file("a.json", b"object, not a level")
    with pytest.raises(ConflictError):
        await storage.put_object("a.json/b.json", b"{}", if_none_match=True)
    assert recorder.methods == ["PUT"] + ["MKCOL", "PUT"] * 3
    assert fake.files["a.json"][0] == b"object, not a level"


async def test_put_onto_a_collection_fails_its_precondition(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    # Observed on Nextcloud 34: If-None-Match: * fails (412) for a folder too.
    fake.add_file("level/inner.json", b"{}")
    with pytest.raises(PreconditionFailedError):
        await storage.put_object("level", b"{}", if_none_match=True)
    assert recorder.methods == ["PUT"]
    assert "level/inner.json" in fake.files


@pytest.mark.parametrize("missing_parent_status", [404, 409])
async def test_put_creates_parents_on_404_and_409(
    storage: NextcloudStorage, recorder: Recorder, missing_parent_status: int
) -> None:
    # Regression: Nextcloud 34 reports a missing parent as 404, not 409.
    puts = iter([missing_parent_status, 201])
    recorder.handler = lambda request: httpx.Response(
        next(puts) if request.method == "PUT" else 201
    )
    await storage.put_object("a/b.json", b"{}", if_none_match=True)
    assert recorder.methods == ["PUT", "MKCOL", "PUT"]


@pytest.mark.parametrize("status", [404, 409])
async def test_put_still_missing_parent_after_mkcol_is_a_conflict(
    storage: NextcloudStorage, recorder: Recorder, status: int
) -> None:
    recorder.handler = lambda request: httpx.Response(
        status if request.method == "PUT" else 405
    )
    with pytest.raises(ConflictError, match="parent level is an object"):
        await storage.put_object("a/b.json", b"{}", if_none_match=True)
    assert recorder.methods == ["PUT"] + ["MKCOL", "PUT"] * 3


async def test_put_retries_while_new_parent_is_briefly_invisible(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    # Regression: concurrent MKCOL answered 405, but the PUT right after it
    # still got 404 on Nextcloud 34.
    puts = iter([404, 404, 201])
    recorder.handler = lambda request: httpx.Response(
        next(puts) if request.method == "PUT" else 405
    )
    info = await storage.put_object("a/b.json", b"{}", if_none_match=True)
    assert info.key == "a/b.json"
    assert recorder.methods == ["PUT", "MKCOL", "PUT", "MKCOL", "PUT"]


async def test_get_object_disables_compression(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    # Regression: with gzip, Apache returned ETag '"<etag>-gzip"' on GET, which
    # never matches If-Match on the next PUT.
    fake.add_file("a.json", b"{}")
    await storage.get_object("a.json")
    assert recorder.last.headers["Accept-Encoding"] == "identity"


async def test_put_404_without_parents_is_not_retried(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    # A top-level key has no parents to create: 404 means the root is missing.
    recorder.handler = lambda request: httpx.Response(404)
    with pytest.raises(NotFoundError):
        await storage.put_object("top.json", b"{}", if_none_match=True)
    assert recorder.methods == ["PUT"]


async def test_put_if_match_never_creates_parents(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = lambda request: httpx.Response(409)
    with pytest.raises(ConflictError):
        await storage.put_object("x/a.json", b"{}", if_match='"e"')
    assert recorder.methods == ["PUT"]


@pytest.mark.parametrize("mkcol_status", [409, 423])
async def test_put_tolerates_concurrent_mkcol_races(
    storage: NextcloudStorage, recorder: Recorder, mkcol_status: int
) -> None:
    # Regression: concurrent MKCOL of the same new level answered 423, and of
    # a child level whose new parent was not visible yet answered 409.
    puts = iter([404, 404, 201])
    recorder.handler = lambda request: httpx.Response(
        next(puts) if request.method == "PUT" else mkcol_status
    )
    await storage.put_object("a/b/c.json", b"{}", if_none_match=True)
    assert recorder.methods == ["PUT"] + ["MKCOL", "MKCOL", "PUT"] * 2


@pytest.mark.parametrize("status", [200, 204])
async def test_put_if_none_match_rejects_replace_status(
    storage: NextcloudStorage, recorder: Recorder, status: int
) -> None:
    recorder.handler = lambda request: httpx.Response(status)
    with pytest.raises(UnexpectedStatusError, match="replaced an existing object"):
        await storage.put_object("a.json", b"{}", if_none_match=True)


async def test_put_if_match_rejects_created_status(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = lambda request: httpx.Response(201)
    with pytest.raises(UnexpectedStatusError):
        await storage.put_object("a.json", b"{}", if_match='"e"')


@pytest.mark.parametrize(
    "conditions",
    [{}, {"if_match": '"e"', "if_none_match": True}, {"if_match": ""}],
)
async def test_put_requires_exactly_one_condition(
    storage: NextcloudStorage, recorder: Recorder, conditions: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="exactly one|non-empty ETag"):
        await storage.put_object("a.json", b"{}", **conditions)
    assert recorder.requests == []


async def test_put_rejects_text(storage: NextcloudStorage, recorder: Recorder) -> None:
    with pytest.raises(TypeError):
        await storage.put_object("a.json", cast(Any, "{}"), if_none_match=True)
    assert recorder.requests == []


@pytest.mark.parametrize("key", ["", "a/", "../a.json", "/abs.json", "a/../../b.json"])
async def test_invalid_keys_are_rejected_before_any_request(
    storage: NextcloudStorage, recorder: Recorder, key: str
) -> None:
    for operation in (
        storage.head_object(key),
        storage.get_object(key),
        storage.put_object(key, b"{}", if_none_match=True),
        storage.delete_object(key),
    ):
        with pytest.raises(InvalidKeyError):
            await operation
    assert recorder.requests == []


async def test_delete_is_guarded_by_observed_etag(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    etag = fake.add_file("p01/a.json", b"1")
    await storage.delete_object("p01/a.json")

    assert recorder.methods == ["PROPFIND", "DELETE"]
    assert recorder.requests[0].headers["Depth"] == "0"
    assert recorder.last.headers["If-Match"] == etag
    assert str(recorder.last.url) == ENCODED_ROOT_URL + "p01/a.json"
    assert "p01/a.json" not in fake.files


async def test_delete_leaves_empty_parent_levels(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    fake.add_file("p01/a.json", b"1")
    await storage.delete_object("p01/a.json")
    assert (await storage.list_objects()).common_prefixes == ("p01/",)


async def test_delete_with_caller_if_match(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    etag = fake.add_file("lock", b"owner-a")
    await storage.delete_object("lock", if_match=etag)
    assert recorder.last.headers["If-Match"] == etag
    assert "lock" not in fake.files


async def test_delete_with_stale_if_match_keeps_object(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    fake.add_file("lock", b"owner-b")
    with pytest.raises(PreconditionFailedError):
        await storage.delete_object("lock", if_match='"stale"')
    assert "lock" in fake.files


async def test_delete_missing_object(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    with pytest.raises(NotFoundError):
        await storage.delete_object("missing.json")


async def test_delete_never_deletes_a_collection(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    fake.add_file("2026/p01/a.json", b"precious")
    with pytest.raises(NotFoundError, match="prefix, not an object"):
        await storage.delete_object("2026")
    assert "DELETE" not in recorder.methods
    assert "2026/p01/a.json" in fake.files


async def test_delete_without_server_etag_is_refused(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = respond_207(multistatus(dav_response(href("a.json"), length=1)))
    with pytest.raises(InvalidResponseError, match="no ETag"):
        await storage.delete_object("a.json")
    assert recorder.methods == ["PROPFIND"]


async def test_delete_rejects_empty_if_match(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    with pytest.raises(ValueError, match="non-empty ETag"):
        await storage.delete_object("a.json", if_match="")
    assert recorder.requests == []


async def test_round_trip_through_fake_server(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    created = await storage.put_object(
        "2026/p01/exp.json", JSON_BYTES, if_none_match=True
    )
    read = await storage.get_object("2026/p01/exp.json")
    head = await storage.head_object("2026/p01/exp.json")

    assert read.data == JSON_BYTES
    assert created.etag == read.info.etag == head.etag
    assert head.size == len(JSON_BYTES)
