import socket
from datetime import UTC, datetime
from typing import Any, Literal

import pytest
from nextcloud_test_support import (
    NAMESPACE_ROOT,
    IntegrationConfig,
    IntegrationConfigError,
    Namespace,
    NamespaceError,
    ObjectManifest,
    cleanup,
    new_run_id,
)
from polari_app.storage import (
    InvalidKeyError,
    NotFoundError,
    ObjectData,
    ObjectInfo,
    ObjectListing,
    PermissionDeniedError,
    PreconditionFailedError,
)

RUN_ID = "20261119T101500Z-0123456789ab"
LOCAL_ROOT = "http://127.0.0.1:18080/remote.php/dav/files/test-user/test-root"
VALID_ENV = {
    "POLARI_INTEGRATION_TARGET": "local",
    "NEXTCLOUD_USERNAME": "test-user",
    "NEXTCLOUD_PASSWORD": "synthetic-password-not-real",
    "NEXTCLOUD_ROOT_PATH": LOCAL_ROOT,
}

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network access attempted in a unit test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_network_guard_is_active() -> None:
    with pytest.raises(AssertionError, match="network access"):
        socket.create_connection(("127.0.0.1", 9))


def test_config_from_valid_env() -> None:
    config = IntegrationConfig.from_env(VALID_ENV)
    assert config.root_url == LOCAL_ROOT
    assert config.username == "test-user"


def test_config_repr_hides_password() -> None:
    config = IntegrationConfig.from_env(VALID_ENV)
    assert VALID_ENV["NEXTCLOUD_PASSWORD"] not in repr(config)
    assert VALID_ENV["NEXTCLOUD_PASSWORD"] not in str(config)


@pytest.mark.parametrize("target", [None, "", "remote", "LOCAL", "ci"])
def test_config_requires_explicit_local_target(target: str | None) -> None:
    env = {k: v for k, v in VALID_ENV.items() if k != "POLARI_INTEGRATION_TARGET"}
    if target is not None:
        env["POLARI_INTEGRATION_TARGET"] = target
    with pytest.raises(IntegrationConfigError, match="must be 'local'"):
        IntegrationConfig.from_env(env)


@pytest.mark.parametrize(
    "var", ["NEXTCLOUD_USERNAME", "NEXTCLOUD_PASSWORD", "NEXTCLOUD_ROOT_PATH"]
)
def test_config_reports_missing_variable_names_only(var: str) -> None:
    env = {**VALID_ENV, var: ""}
    with pytest.raises(IntegrationConfigError) as info:
        IntegrationConfig.from_env(env)
    assert var in str(info.value)
    assert VALID_ENV["NEXTCLOUD_PASSWORD"] not in str(info.value)


@pytest.mark.parametrize(
    "root_url",
    [
        "http://127.0.0.1:18080/remote.php/dav/files/u/root",
        "https://127.0.0.1:8443/dav/root/",
        "http://[::1]:18080/remote.php/dav/files/u/my root",
        "http://127.0.0.2:18080/x",  # all of 127.0.0.0/8 is loopback
    ],
)
def test_config_accepts_loopback_roots(root_url: str) -> None:
    IntegrationConfig.from_env({**VALID_ENV, "NEXTCLOUD_ROOT_PATH": root_url})


@pytest.mark.parametrize(
    "root_url",
    [
        "https://cloud.example.org/remote.php/dav/files/u/root",  # remote host
        "http://localhost:18080/remote.php/dav/files/u/root",  # name, not IP
        "http://192.168.1.10:18080/dav/root",  # private but not loopback
        "http://0.0.0.0:18080/dav/root",
        "http://127.0.0.1/remote.php/dav/files/u/root",  # no explicit port
        "http://127.0.0.1:18080/",  # server root
        "http://127.0.0.1:18080",
        "http://127.0.0.1:18080/dav/../root",
        "http://u:p@127.0.0.1:18080/dav/root",
        "http://127.0.0.1:18080/dav/root?x=1",
        "ftp://127.0.0.1:18080/dav/root",
        "/remote.php/dav/files/u/root",  # a bare path
        "http://127.0.0.1:99999/dav/root",  # invalid port
    ],
)
def test_config_rejects_non_local_or_unsafe_roots(root_url: str) -> None:
    with pytest.raises(IntegrationConfigError):
        IntegrationConfig.from_env({**VALID_ENV, "NEXTCLOUD_ROOT_PATH": root_url})


def test_new_run_id_format_and_uniqueness() -> None:
    moment = datetime(2026, 11, 19, 10, 15, 0, tzinfo=UTC)
    first, second = new_run_id(moment), new_run_id(moment)
    assert first.startswith("20261119T101500Z-")
    assert first != second
    Namespace.for_run(first)  # valid


@pytest.mark.parametrize(
    "run_id",
    ["", "x", "../evil", "20261119T101500Z", "20261119T101500Z-XYZ", RUN_ID + "/x"],
)
def test_namespace_rejects_invalid_run_ids(run_id: str) -> None:
    with pytest.raises(NamespaceError):
        Namespace.for_run(run_id)


def test_namespace_layout() -> None:
    namespace = Namespace.for_run(RUN_ID)
    assert namespace.prefix == f"{NAMESPACE_ROOT}{RUN_ID}/"
    child = namespace.child("t-0001")
    assert child.prefix == f"{NAMESPACE_ROOT}{RUN_ID}/t-0001/"
    assert child.key("a b/ü.json") == f"{NAMESPACE_ROOT}{RUN_ID}/t-0001/a b/ü.json"


@pytest.mark.parametrize(
    "prefix",
    ["", "/", "integration-tests/", "other/", "integration-tests", "../x/", "a//"],
)
def test_namespace_cannot_be_the_root_or_outside(prefix: str) -> None:
    with pytest.raises((NamespaceError, InvalidKeyError)):
        Namespace(prefix)


@pytest.mark.parametrize("name", ["", "A", "a/b", "..", ".", "-a", "a" * 64, "a b"])
def test_namespace_child_names_are_restricted(name: str) -> None:
    with pytest.raises(NamespaceError):
        Namespace.for_run(RUN_ID).child(name)


@pytest.mark.parametrize(
    "relative", ["", "../x", "a/../../x", "/abs", "a//b", "a/", "..", "a\\b"]
)
def test_namespace_keys_cannot_escape(relative: str) -> None:
    with pytest.raises(InvalidKeyError):
        Namespace.for_run(RUN_ID).key(relative)


def test_namespace_ownership() -> None:
    namespace = Namespace.for_run(RUN_ID).child("t-1")
    assert namespace.owns(namespace.key("a.json"))
    assert not namespace.owns(f"{NAMESPACE_ROOT}{RUN_ID}/t-2/a.json")
    assert not namespace.owns(f"{NAMESPACE_ROOT}{RUN_ID}/t-1")  # the level itself
    assert not namespace.owns("experiments_data/2026/p01/experiment.json")
    assert not namespace.owns(namespace.prefix + "../../escape")
    assert not namespace.owns("")


def test_manifest_refuses_foreign_keys() -> None:
    manifest = ObjectManifest(Namespace.for_run(RUN_ID).child("t-1"))
    with pytest.raises(NamespaceError):
        manifest.record(ObjectInfo("experiments_data/real.json", 1, '"e"'))
    assert len(manifest) == 0


class RecordingStorage:
    """Fake ``ObjectStorage`` for cleanup tests; only ``delete_object`` works."""

    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.deletes: list[tuple[str, str | None]] = []
        self.failures = failures or {}

    async def delete_object(self, key: str, *, if_match: str | None = None) -> None:
        self.deletes.append((key, if_match))
        if key in self.failures:
            raise self.failures[key]

    async def list_objects(
        self, prefix: str = "", *, delimiter: Literal["/"] | None = "/"
    ) -> ObjectListing:
        raise AssertionError("cleanup must not list")

    async def head_object(self, key: str) -> ObjectInfo:
        raise AssertionError("not used")

    async def get_object(self, key: str) -> ObjectData:
        raise AssertionError("not used")

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> ObjectInfo:
        raise AssertionError("cleanup must not write")


async def test_cleanup_deletes_only_recorded_objects_with_their_etags() -> None:
    namespace = Namespace.for_run(RUN_ID).child("t-1")
    manifest = ObjectManifest(namespace)
    manifest.record(ObjectInfo(namespace.key("a.json"), 1, '"e1"'))
    manifest.record(ObjectInfo(namespace.key("dir/b.json"), 1, None))
    storage = RecordingStorage()

    report = await cleanup(storage, manifest)

    assert storage.deletes == [
        (namespace.key("a.json"), '"e1"'),
        (namespace.key("dir/b.json"), None),  # storage guards with observed ETag
    ]
    assert report.deleted == [namespace.key("a.json"), namespace.key("dir/b.json")]
    assert report.complete
    assert len(manifest) == 0


async def test_cleanup_uses_latest_recorded_etag() -> None:
    namespace = Namespace.for_run(RUN_ID).child("t-1")
    manifest = ObjectManifest(namespace)
    manifest.record(ObjectInfo(namespace.key("a.json"), 1, '"v1"'))
    manifest.record(ObjectInfo(namespace.key("a.json"), 2, '"v2"'))
    storage = RecordingStorage()
    await cleanup(storage, manifest)
    assert storage.deletes == [(namespace.key("a.json"), '"v2"')]


async def test_cleanup_reports_failures_and_continues() -> None:
    namespace = Namespace.for_run(RUN_ID).child("t-1")
    gone, changed, denied, ok = (namespace.key(n) for n in ("g", "c", "d", "o"))
    manifest = ObjectManifest(namespace)
    for key in (gone, changed, denied, ok):
        manifest.record(ObjectInfo(key, 1, '"e"'))
    storage = RecordingStorage(
        {
            gone: NotFoundError("DELETE", gone, 404),
            changed: PreconditionFailedError("DELETE", changed, 412),
            denied: PermissionDeniedError("DELETE", denied, 403),
        }
    )

    report = await cleanup(storage, manifest)

    assert report.already_missing == [gone]
    assert report.failed == [
        (changed, "PreconditionFailedError"),
        (denied, "PermissionDeniedError"),
    ]
    assert report.deleted == [ok]
    assert not report.complete


async def test_cleanup_refuses_keys_smuggled_into_the_manifest() -> None:
    namespace = Namespace.for_run(RUN_ID).child("t-1")
    manifest = ObjectManifest(namespace)
    manifest._objects["experiments_data/real.json"] = '"e"'  # bypass record()
    storage = RecordingStorage()

    report = await cleanup(storage, manifest)

    assert storage.deletes == []
    assert report.failed == [("experiments_data/real.json", "NamespaceError")]


async def test_cleanup_of_empty_manifest_does_nothing() -> None:
    storage = RecordingStorage()
    report = await cleanup(storage, ObjectManifest(Namespace.for_run(RUN_ID)))
    assert storage.deletes == []
    assert report.complete
