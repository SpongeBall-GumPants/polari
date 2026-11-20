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
    InvalidKeyError,
    InvalidResponseError,
    NextcloudStorage,
    ObjectInfo,
    UnexpectedStatusError,
)
from polari_app.storage.propfind import PROPFIND_BODY

pytestmark = pytest.mark.anyio

TRIAL = "2026/p01/2026-11-14/trial_1/"


def trial_level() -> bytes:
    return multistatus(
        dav_response(href(TRIAL.rstrip("/"), collection=True), collection=True),
        dav_response(href(TRIAL + "mapping.yaml"), length=120, etag='"m1"'),
        dav_response(href(TRIAL + "calibration", collection=True), collection=True),
        dav_response(href(TRIAL + "data.xdf"), length=1_048_576, etag='"x1"'),
    )


async def test_delimiter_listing_sends_one_depth_1_propfind(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = respond_207(trial_level())
    await storage.list_objects(TRIAL)

    assert len(recorder.requests) == 1
    request = recorder.last
    assert request.method == "PROPFIND"
    assert request.headers["Depth"] == "1"
    assert request.headers["Content-Type"].startswith("application/xml")
    assert request.content == PROPFIND_BODY
    assert str(request.url) == ENCODED_ROOT_URL + TRIAL


async def test_delimiter_listing_splits_objects_and_common_prefixes(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = respond_207(trial_level())
    listing = await storage.list_objects(TRIAL)

    assert set(listing.objects) == {
        ObjectInfo(TRIAL + "mapping.yaml", 120, '"m1"'),
        ObjectInfo(TRIAL + "data.xdf", 1_048_576, '"x1"'),
    }
    assert listing.common_prefixes == (TRIAL + "calibration/",)


async def test_listing_root(storage: NextcloudStorage, recorder: Recorder) -> None:
    recorder.handler = respond_207(
        multistatus(
            dav_response(href(""), collection=True),
            dav_response(href("2026", collection=True), collection=True),
            dav_response(href("README.txt"), length=3),
        )
    )
    listing = await storage.list_objects()
    assert str(recorder.last.url) == ENCODED_ROOT_URL
    assert listing.objects == (ObjectInfo("README.txt", 3, None),)
    assert listing.common_prefixes == ("2026/",)


async def test_missing_prefix_gives_empty_listing(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = lambda request: httpx.Response(404)
    listing = await storage.list_objects("does/not/exist/")
    assert listing.objects == ()
    assert listing.common_prefixes == ()


async def test_prefix_naming_an_object_gives_empty_listing(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = respond_207(multistatus(dav_response(href("a.json"), length=2)))
    listing = await storage.list_objects("a.json/")
    assert listing.objects == ()
    assert listing.common_prefixes == ()


async def test_empty_collection_still_appears_as_common_prefix(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    # Documented Nextcloud difference to S3: empty levels stick around.
    fake.collections.add("old")
    assert (await storage.list_objects()).common_prefixes == ("old/",)
    assert (await storage.list_objects("old/")).objects == ()


async def test_unicode_and_spaces_in_keys(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = respond_207(
        multistatus(
            dav_response(href("my data", collection=True), collection=True),
            dav_response(href("my data/über uns.json"), length=2),
        )
    )
    listing = await storage.list_objects("my data/")
    assert recorder.last.url.raw_path.endswith(b"/polari%20root/my%20data/")
    assert listing.objects == (ObjectInfo("my data/über uns.json", 2, None),)


async def test_absolute_url_hrefs_are_accepted(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    host = "https://dav.example.test"
    recorder.handler = respond_207(
        multistatus(
            dav_response(host + href("d", collection=True), collection=True),
            dav_response(host + href("d/x.json"), length=1),
        )
    )
    assert (await storage.list_objects("d/")).objects == (
        ObjectInfo("d/x.json", 1, None),
    )


async def test_recursive_listing_walks_all_levels(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    fake.add_file("2026/p01/a.json", b"1")
    fake.add_file("2026/p01/trial_1/mapping.yaml", b"22")
    fake.add_file("2026/p02/b.json", b"333")
    fake.add_file("other.json", b"4444")
    fake.collections.add("2026/empty")

    listing = await storage.list_objects("2026/", delimiter=None)

    assert {(o.key, o.size) for o in listing.objects} == {
        ("2026/p01/a.json", 1),
        ("2026/p01/trial_1/mapping.yaml", 2),
        ("2026/p02/b.json", 3),
    }
    assert listing.common_prefixes == ()  # empty levels stay hidden
    assert set(recorder.methods) == {"PROPFIND"}
    assert all(r.headers["Depth"] == "1" for r in recorder.requests)
    assert len(recorder.requests) == 5  # 2026/, p01/, p01/trial_1/, p02/, empty/


async def test_recursive_listing_of_missing_prefix_is_empty(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    listing = await storage.list_objects("nothing/", delimiter=None)
    assert listing.objects == ()


async def test_recursive_listing_tolerates_level_deleted_during_walk(
    storage: NextcloudStorage, fake: FakeNextcloud, recorder: Recorder
) -> None:
    fake.add_file("a/keep.json", b"1")
    fake.add_file("a/gone/x.json", b"2")

    def vanishing(request: httpx.Request) -> httpx.Response:
        if "gone" in request.url.path:  # removed by someone else meanwhile
            return httpx.Response(404)
        return fake(request)

    recorder.handler = vanishing
    listing = await storage.list_objects("a/", delimiter=None)
    assert [o.key for o in listing.objects] == ["a/keep.json"]


@pytest.mark.parametrize("prefix", ["2026", "2026/p0", "/2026/", "../", "a//"])
async def test_invalid_prefix_is_rejected_before_any_request(
    storage: NextcloudStorage, recorder: Recorder, prefix: str
) -> None:
    with pytest.raises(InvalidKeyError):
        await storage.list_objects(prefix)
    assert recorder.requests == []


async def test_invalid_delimiter_is_rejected(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    with pytest.raises(ValueError, match="delimiter"):
        await storage.list_objects("", delimiter=cast(Any, ","))
    assert recorder.requests == []


@pytest.mark.parametrize(
    "extra",
    [
        dav_response(href("d/sub/deeper.json"), length=1),  # not a direct child
        dav_response(href("other/x.json"), length=1),  # sibling level
        dav_response("/remote.php/dav/files/other-user/x.json", length=1),
        dav_response(href("d", collection=True), collection=True),  # duplicate self
        dav_response(href(""), collection=True),  # root inside a listing of "d/"
    ],
)
async def test_unexpected_entries_are_invalid(
    storage: NextcloudStorage, recorder: Recorder, extra: str
) -> None:
    recorder.handler = respond_207(
        multistatus(dav_response(href("d", collection=True), collection=True), extra)
    )
    with pytest.raises(InvalidResponseError):
        await storage.list_objects("d/")


async def test_missing_self_entry_is_invalid(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = respond_207(
        multistatus(dav_response(href("d/x.json"), length=1))
    )
    with pytest.raises(InvalidResponseError, match="listed level missing"):
        await storage.list_objects("d/")


async def test_malformed_xml_is_invalid(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = respond_207(b"<d:multistatus xmlns:d='DAV:'>")
    with pytest.raises(InvalidResponseError, match="PROPFIND 'd/'"):
        await storage.list_objects("d/")


async def test_200_instead_of_207_is_unexpected(
    storage: NextcloudStorage, recorder: Recorder
) -> None:
    recorder.handler = lambda request: httpx.Response(200, text="<html>login</html>")
    with pytest.raises(UnexpectedStatusError) as info:
        await storage.list_objects("d/")
    assert info.value.status_code == 200
