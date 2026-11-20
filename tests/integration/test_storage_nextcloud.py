import asyncio
import json
import socket

import pytest
from nextcloud_test_support import IntegrationConfig, Namespace, ObjectManifest
from polari_app.storage import (
    AuthenticationError,
    ConflictError,
    LockedError,
    NextcloudStorage,
    NotFoundError,
    ObjectInfo,
    PreconditionFailedError,
    StorageConnectionError,
    UnexpectedStatusError,
)

pytestmark = pytest.mark.anyio

SAME_SECOND_ETAG = pytest.mark.xfail(
    strict=True,
    reason=(
        "Nextcloud 34.0.6 keeps the ETag when an object is updated within the "
        "same second, so a stale If-Match still succeeds (lost update). Root "
        "cause: the file cache reuses the ETag while the mtime second is unchanged."
    ),
)


async def next_second() -> None:
    """Make the next write land in a later second than the previous one.

    Nextcloud's ETag only changes when the file's mtime (whole seconds)
    changes. Tests of the If-Match mechanism itself wait here, so they do not
    depend on that limitation; it is tested on its own below.
    """
    await asyncio.sleep(1.1)


def refused_write(result: object) -> bool:
    """A concurrent write that lost: 412 (condition) or 423 (file locked)."""
    return isinstance(result, PreconditionFailedError | LockedError)


def synthetic_json(index: int) -> bytes:
    document = {
        "schema_version": 1,
        "title": f"Synthetic integration test {index}",
        "pilot": "synthetic-p00",
        "notes": "Ünïcödé ✓ — not real data",
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True).encode()


async def put_new(
    storage: NextcloudStorage, manifest: ObjectManifest, key: str, data: bytes
) -> ObjectInfo:
    return manifest.record(await storage.put_object(key, data, if_none_match=True))


async def test_create_and_read_back_exact_bytes(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    payloads = {
        "experiment.json": synthetic_json(1),
        "binary.bin": bytes(range(256)) * 8 + b"\x00\r\n\xff",
        "empty.txt": b"",
    }
    for name, data in payloads.items():
        await put_new(storage, manifest, namespace.key(name), data)
    for name, data in payloads.items():
        result = await storage.get_object(namespace.key(name))
        assert result.data == data
        assert result.info.size == len(data)


async def test_metadata_and_etag_are_consistent(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    key = namespace.key("meta.json")
    data = synthetic_json(2)
    created = await put_new(storage, manifest, key, data)

    head = await storage.head_object(key)
    read = await storage.get_object(key)

    assert head == ObjectInfo(key, len(data), head.etag)
    assert head.etag
    assert created.etag == head.etag == read.info.etag


async def test_etag_changes_on_update_and_cas_succeeds(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    key = namespace.key("cas.json")
    first = await put_new(storage, manifest, key, b'{"revision": 1}')
    assert first.etag is not None
    await next_second()

    second = manifest.record(
        await storage.put_object(key, b'{"revision": 2}', if_match=first.etag)
    )

    assert (await storage.get_object(key)).data == b'{"revision": 2}'
    head = await storage.head_object(key)
    assert head.etag != first.etag
    assert second.etag == head.etag


@SAME_SECOND_ETAG
async def test_etag_changes_on_every_update(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    # Required for safe compare-and-swap: a new version needs a new ETag,
    # even if it is written right after the previous one.
    for attempt in range(5):
        key = namespace.key(f"quick-update/{attempt}.json")
        first = await put_new(storage, manifest, key, b"one")
        second = manifest.record(
            await storage.put_object(key, b"two-different-size", if_match=first.etag)
        )
        assert second.etag != first.etag, f"ETag unchanged on attempt {attempt}"


async def test_stale_etag_is_rejected(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    key = namespace.key("stale.json")
    first = await put_new(storage, manifest, key, b"v1")
    assert first.etag is not None
    await next_second()
    manifest.record(await storage.put_object(key, b"v2", if_match=first.etag))

    with pytest.raises(PreconditionFailedError):
        await storage.put_object(key, b"v3-lost-update", if_match=first.etag)
    assert (await storage.get_object(key)).data == b"v2"


@SAME_SECOND_ETAG
async def test_stale_writer_right_after_an_update_is_rejected(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    # The practical impact of the ETag limitation: a lost update.
    for attempt in range(5):
        key = namespace.key(f"lost-update/{attempt}.json")
        first = await put_new(storage, manifest, key, b"v1")
        manifest.record(await storage.put_object(key, b"v2", if_match=first.etag))
        try:
            stale = await storage.put_object(key, b"stale", if_match=first.etag)
        except PreconditionFailedError:
            continue
        manifest.record(stale)
        pytest.fail(f"stale writer overwrote v2 on attempt {attempt}")


async def test_if_match_on_missing_object_is_rejected(
    storage: NextcloudStorage, namespace: Namespace
) -> None:
    key = namespace.key("never-created.json")
    with pytest.raises(PreconditionFailedError):
        await storage.put_object(key, b"x", if_match='"does-not-exist"')
    with pytest.raises(NotFoundError):
        await storage.head_object(key)


async def test_create_only_never_overwrites(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    key = namespace.key("create-only.json")
    await put_new(storage, manifest, key, b"original")

    with pytest.raises(PreconditionFailedError):
        await storage.put_object(key, b"replacement", if_none_match=True)
    assert (await storage.get_object(key)).data == b"original"


async def test_parent_collections_are_created_and_hidden(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    key = namespace.key("2026/p00/2026-11-19/trial_1/mapping.yaml")
    await put_new(storage, manifest, key, b"channels: []\n")

    assert (await storage.get_object(key)).data == b"channels: []\n"
    level = await storage.list_objects(namespace.prefix + "2026/p00/")
    assert level.objects == ()
    assert level.common_prefixes == (namespace.prefix + "2026/p00/2026-11-19/",)
    with pytest.raises(NotFoundError):
        await storage.head_object(namespace.key("2026/p00"))


async def test_listing_with_delimiter_and_recursively(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    names = ["a.json", "b c.json", "sub/d.json", "sub/deeper/e.json"]
    for index, name in enumerate(names):
        await put_new(storage, manifest, namespace.key(f"list/{name}"), b"x" * index)
    base = namespace.prefix + "list/"

    level = await storage.list_objects(base)
    assert {(o.key, o.size) for o in level.objects} == {
        (base + "a.json", 0),
        (base + "b c.json", 1),
    }
    assert all(o.etag for o in level.objects)
    assert level.common_prefixes == (base + "sub/",)

    everything = await storage.list_objects(base, delimiter=None)
    assert {o.key for o in everything.objects} == {base + n for n in names}
    assert everything.common_prefixes == ()


async def test_missing_prefix_and_object_prefix_list_empty(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    missing = await storage.list_objects(namespace.prefix + "no/such/level/")
    assert (missing.objects, missing.common_prefixes) == ((), ())
    missing_walk = await storage.list_objects(
        namespace.prefix + "no/such/level/", delimiter=None
    )
    assert missing_walk.objects == ()

    await put_new(storage, manifest, namespace.key("file.json"), b"{}")
    below_object = await storage.list_objects(namespace.prefix + "file.json/")
    assert (below_object.objects, below_object.common_prefixes) == ((), ())


@pytest.mark.parametrize(
    "name",
    [
        "Ünïcödé dir/çalışma ğüş.json",
        "spaces  double/x y.json",
        "percent%20literal.json",
        "hash#and?question.json",
        "emoji 🧠.json",
        "brackets [1] (2) {3}.json",
    ],
)
async def test_unicode_and_special_characters_in_keys(
    storage: NextcloudStorage,
    namespace: Namespace,
    manifest: ObjectManifest,
    name: str,
) -> None:
    key = namespace.key(name)
    data = name.encode()
    await put_new(storage, manifest, key, data)

    assert (await storage.get_object(key)).data == data
    parent = namespace.prefix + (name.rpartition("/")[0] + "/" if "/" in name else "")
    listing = await storage.list_objects(parent)
    assert key in {o.key for o in listing.objects}


async def test_decomposed_unicode_key_is_normalized(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    decomposed = namespace.prefix + "Müller.json"  # macOS-style NFD
    created = await put_new(storage, manifest, decomposed, b"nfd")
    assert created.key == namespace.prefix + "Müller.json"
    listing = await storage.list_objects(namespace.prefix)
    assert [o.key for o in listing.objects] == [created.key]
    assert (await storage.get_object(decomposed)).data == b"nfd"


async def test_missing_object(storage: NextcloudStorage, namespace: Namespace) -> None:
    key = namespace.key("missing.json")
    with pytest.raises(NotFoundError):
        await storage.head_object(key)
    with pytest.raises(NotFoundError):
        await storage.get_object(key)
    with pytest.raises(NotFoundError):
        await storage.delete_object(key)


async def test_get_object_on_a_collection_is_not_found(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    await put_new(storage, manifest, namespace.key("level/inner.json"), b"{}")
    with pytest.raises(NotFoundError):
        await storage.get_object(namespace.key("level"))


async def test_put_onto_a_collection_fails_its_precondition(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    await put_new(storage, manifest, namespace.key("level/inner.json"), b"{}")
    with pytest.raises(PreconditionFailedError):
        await storage.put_object(namespace.key("level"), b"x", if_none_match=True)
    assert (await storage.get_object(namespace.key("level/inner.json"))).data == b"{}"


async def test_put_below_an_object_is_a_conflict(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    await put_new(storage, manifest, namespace.key("leaf.json"), b"leaf")
    with pytest.raises(ConflictError):
        await storage.put_object(
            namespace.key("leaf.json/child.json"), b"x", if_none_match=True
        )
    assert (await storage.get_object(namespace.key("leaf.json"))).data == b"leaf"


def reported_replace(result: object) -> bool:
    """The client was told the server overwrote despite If-None-Match: *."""
    return isinstance(result, UnexpectedStatusError) and result.status_code in (
        200,
        204,
    )


async def race_creates(
    storage: NextcloudStorage, manifest: ObjectManifest, key: str, writers: int
) -> list[object]:
    results: list[object] = list(
        await asyncio.gather(
            *(
                storage.put_object(key, f"writer-{i}".encode(), if_none_match=True)
                for i in range(writers)
            ),
            return_exceptions=True,
        )
    )
    if any(isinstance(r, ObjectInfo) or reported_replace(r) for r in results):
        manifest.record(await storage.head_object(key))  # track for cleanup
    return results


async def test_concurrent_creates_never_overwrite_silently(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    # Safety property of the client: at most one writer is told "created";
    # every other writer is refused (412/423) or told that it replaced data.
    for trial in range(3):
        key = namespace.key(f"race/create-{trial}.json")
        results = await race_creates(storage, manifest, key, writers=8)
        winners = [i for i, r in enumerate(results) if isinstance(r, ObjectInfo)]
        replacers = [i for i, r in enumerate(results) if reported_replace(r)]
        others = [
            r for i, r in enumerate(results) if i not in winners and i not in replacers
        ]

        assert len(winners) <= 1, results
        assert all(refused_write(r) for r in others), others
        owners = replacers or winners
        if owners:
            stored = (await storage.get_object(key)).data
            assert stored in {f"writer-{i}".encode() for i in owners}
        else:
            with pytest.raises(NotFoundError):
                await storage.head_object(key)


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Nextcloud 34.0.6 does not apply If-None-Match: * atomically: under "
        "concurrent creates a PUT sometimes answers 204 and replaces the object "
        "another writer just created (seen in about 1 of 6 trials)."
    ),
)
async def test_create_only_is_atomic_under_concurrency(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    for trial in range(5):
        key = namespace.key(f"race/atomic-{trial}.json")
        results = await race_creates(storage, manifest, key, writers=8)
        assert not any(reported_replace(r) for r in results), results


async def test_concurrent_creates_under_one_new_parent_all_succeed(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    keys = [namespace.key(f"fresh/level/trial_{i}/data.json") for i in range(6)]
    results = await asyncio.gather(
        *(storage.put_object(key, b"{}", if_none_match=True) for key in keys),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, ObjectInfo):
            manifest.record(result)
    assert all(isinstance(r, ObjectInfo) for r in results), results
    listing = await storage.list_objects(namespace.prefix + "fresh/", delimiter=None)
    assert {o.key for o in listing.objects} == set(keys)


async def test_concurrent_compare_and_swap_has_at_most_one_winner(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    # Safety: never more than one winner, losers are refused (412/423), and
    # the stored value is the winner's. Liveness is not guaranteed: with
    # Nextcloud file locking all writers can get 423 (seen in local runs).
    key = namespace.key("race/counter.json")
    base = await put_new(storage, manifest, key, b"0")
    assert base.etag is not None
    await next_second()

    results = await asyncio.gather(
        *(
            storage.put_object(key, f"1-from-{i}".encode(), if_match=base.etag)
            for i in range(5)
        ),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, ObjectInfo)]
    losers = [r for r in results if not isinstance(r, ObjectInfo)]
    for winner in winners:
        manifest.record(winner)

    assert len(winners) <= 1, results
    assert all(refused_write(r) for r in losers), losers
    stored = await storage.get_object(key)
    expected = f"1-from-{results.index(winners[0])}" if winners else "0"
    assert stored.data == expected.encode()


async def test_conditional_delete(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    key = namespace.key("delete/me.json")
    created = await put_new(storage, manifest, key, b"v1")
    await next_second()
    updated = manifest.record(
        await storage.put_object(key, b"v2", if_match=created.etag)
    )

    with pytest.raises(PreconditionFailedError):
        await storage.delete_object(key, if_match=created.etag)
    assert (await storage.get_object(key)).data == b"v2"

    await storage.delete_object(key, if_match=updated.etag)
    manifest.forget(key)
    with pytest.raises(NotFoundError):
        await storage.head_object(key)


async def test_delete_without_etag_uses_observed_etag(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    key = namespace.key("delete/observed.json")
    await put_new(storage, manifest, key, b"x")
    await storage.delete_object(key)
    manifest.forget(key)
    with pytest.raises(NotFoundError):
        await storage.get_object(key)


async def test_delete_never_removes_a_collection(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    inner = namespace.key("keep/inner.json")
    await put_new(storage, manifest, inner, b"precious")
    with pytest.raises(NotFoundError):
        await storage.delete_object(namespace.key("keep"))
    assert (await storage.get_object(inner)).data == b"precious"


async def test_empty_levels_remain_after_delete(
    storage: NextcloudStorage, namespace: Namespace, manifest: ObjectManifest
) -> None:
    key = namespace.key("ephemeral/only.json")
    await put_new(storage, manifest, key, b"x")
    await storage.delete_object(key)
    manifest.forget(key)

    listing = await storage.list_objects(namespace.prefix)
    assert listing.common_prefixes == (namespace.prefix + "ephemeral/",)
    assert (await storage.list_objects(namespace.prefix + "ephemeral/")).objects == ()


async def test_wrong_password_is_authentication_error(
    integration_config: IntegrationConfig, namespace: Namespace
) -> None:
    # One deliberate failure only: Nextcloud throttles repeated bad logins.
    async with NextcloudStorage(
        integration_config.root_url,
        integration_config.username,
        "synthetic-wrong-password",
    ) as storage:
        with pytest.raises(AuthenticationError):
            await storage.head_object(namespace.key("any.json"))


async def test_connection_refused_is_typed(
    integration_config: IntegrationConfig, namespace: Namespace
) -> None:
    with socket.socket() as probe:  # find a free loopback port, then release it
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    async with NextcloudStorage(
        f"http://127.0.0.1:{port}/remote.php/dav/files/nobody/root", "x", "y"
    ) as storage:
        with pytest.raises(StorageConnectionError):
            await storage.list_objects(namespace.prefix)
