import asyncio

import pytest
from dav_helpers import FakeNextcloud
from polari_app.storage import NextcloudStorage, PreconditionFailedError

pytestmark = pytest.mark.anyio


async def test_concurrent_creates_of_same_key_have_one_winner(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    results = await asyncio.gather(
        *(
            storage.put_object("lock", f"owner-{i}".encode(), if_none_match=True)
            for i in range(5)
        ),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, BaseException)]

    assert len(winners) == 1
    assert len(losers) == 4
    assert all(isinstance(r, PreconditionFailedError) for r in losers)
    assert fake.files["lock"][1] == winners[0].etag


async def test_concurrent_creates_under_same_new_parent_all_succeed(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    keys = [f"2026/p01/trial_{i}/data.json" for i in range(4)]
    await asyncio.gather(
        *(storage.put_object(key, b"{}", if_none_match=True) for key in keys)
    )
    # Racing MKCOLs answer 405 for the losers; that must not be an error.
    assert all(key in fake.files for key in keys)


async def test_concurrent_compare_and_swap_has_one_winner(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    etag = fake.add_file("counter.json", b"0")
    results = await asyncio.gather(
        storage.put_object("counter.json", b"1-from-a", if_match=etag),
        storage.put_object("counter.json", b"1-from-b", if_match=etag),
        return_exceptions=True,
    )
    assert sum(isinstance(r, PreconditionFailedError) for r in results) == 1
    assert fake.files["counter.json"][0] in (b"1-from-a", b"1-from-b")


async def test_read_modify_write_detects_concurrent_change(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    fake.add_file("exp.json", b'{"revision": 1}')
    mine = await storage.get_object("exp.json")

    other = await storage.get_object("exp.json")
    await storage.put_object("exp.json", b'{"revision": 2}', if_match=other.info.etag)

    with pytest.raises(PreconditionFailedError):
        await storage.put_object(
            "exp.json", b'{"revision": 2, "mine": 1}', if_match=mine.info.etag
        )
    assert fake.files["exp.json"][0] == b'{"revision": 2}'


async def test_delete_and_update_race_has_one_winner(
    storage: NextcloudStorage, fake: FakeNextcloud
) -> None:
    etag = fake.add_file("lock", b"owner-a")
    results = await asyncio.gather(
        storage.delete_object("lock", if_match=etag),
        storage.put_object("lock", b"owner-b", if_match=etag),
        return_exceptions=True,
    )
    assert sum(isinstance(r, BaseException) for r in results) == 1


async def test_concurrent_reads(storage: NextcloudStorage, fake: FakeNextcloud) -> None:
    for i in range(10):
        fake.add_file(f"r/{i}.json", str(i).encode())
    results = await asyncio.gather(
        *(storage.get_object(f"r/{i}.json") for i in range(10))
    )
    assert [r.data for r in results] == [str(i).encode() for i in range(10)]
