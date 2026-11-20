import ipaddress
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from polari_app.storage import NotFoundError, ObjectInfo, ObjectStorage, StorageError
from polari_app.storage.keys import validate_key, validate_prefix

TARGET_VAR = "POLARI_INTEGRATION_TARGET"
CONFIG_VARS = ("NEXTCLOUD_USERNAME", "NEXTCLOUD_PASSWORD", "NEXTCLOUD_ROOT_PATH")
NAMESPACE_ROOT = "integration-tests/"

_RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
_CHILD_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


class IntegrationConfigError(Exception):
    """The integration test configuration is missing or not allowed."""


class NamespaceError(Exception):
    """A key or namespace would leave the test run's own namespace."""


@dataclass(frozen=True, slots=True)
class IntegrationConfig:
    """Validated settings for running integration tests.

    Only the local mode exists: the root URL must point to a loopback IP
    address. The password is excluded from ``repr`` so it never ends up in
    tracebacks or pytest output.
    """

    root_url: str
    username: str
    password: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> IntegrationConfig:
        target = env.get(TARGET_VAR, "")
        if target != "local":
            raise IntegrationConfigError(
                f"{TARGET_VAR} must be 'local'. Running against a remote "
                "Nextcloud is not enabled; that needs a maintainer decision."
            )
        missing = [name for name in CONFIG_VARS if not env.get(name)]
        if missing:
            raise IntegrationConfigError(f"Missing variables: {', '.join(missing)}")
        root_url = env["NEXTCLOUD_ROOT_PATH"]
        validate_local_root_url(root_url)
        return cls(root_url, env["NEXTCLOUD_USERNAME"], env["NEXTCLOUD_PASSWORD"])


def validate_local_root_url(root_url: str) -> None:
    """Accept only ``http(s)://<loopback IP>:<port>/<at least one level>``.

    Host names (even ``localhost``) are rejected, so no DNS lookup can
    redirect the tests to another machine.
    """
    try:
        parts = urlsplit(root_url)
        port = parts.port
    except ValueError as exc:
        raise IntegrationConfigError("NEXTCLOUD_ROOT_PATH is not a valid URL") from exc
    if parts.scheme not in ("http", "https"):
        raise IntegrationConfigError("NEXTCLOUD_ROOT_PATH must be an http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise IntegrationConfigError("NEXTCLOUD_ROOT_PATH must not contain credentials")
    if parts.query or parts.fragment:
        raise IntegrationConfigError("NEXTCLOUD_ROOT_PATH must not contain '?' or '#'")
    try:
        address = ipaddress.ip_address(parts.hostname or "")
    except ValueError as exc:
        raise IntegrationConfigError(
            "NEXTCLOUD_ROOT_PATH must use a loopback IP address (e.g. 127.0.0.1)"
        ) from exc
    if not address.is_loopback:
        raise IntegrationConfigError("NEXTCLOUD_ROOT_PATH must point to loopback")
    if port is None:
        raise IntegrationConfigError("NEXTCLOUD_ROOT_PATH must name an explicit port")
    levels = [level for level in parts.path.split("/") if level]
    if not levels:
        raise IntegrationConfigError("NEXTCLOUD_ROOT_PATH must not be the server root")
    if any(level in (".", "..") for level in levels):
        raise IntegrationConfigError("NEXTCLOUD_ROOT_PATH must not contain '.'/'..'")


def new_run_id(now: datetime | None = None) -> str:
    """``20261119T101500Z-<12 hex>``: sortable and unique per run."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"{moment:%Y%m%dT%H%M%SZ}-{secrets.token_hex(6)}"


@dataclass(frozen=True, slots=True)
class Namespace:
    """A key prefix that the current test run owns exclusively.

    Every key handed to the storage in integration tests comes from
    :meth:`key`, so a test cannot address anything outside its namespace.
    """

    prefix: str

    @classmethod
    def for_run(cls, run_id: str) -> Namespace:
        if not _RUN_ID.fullmatch(run_id):
            raise NamespaceError(f"Invalid run id: {run_id!r}")
        return cls(validate_prefix(f"{NAMESPACE_ROOT}{run_id}/"))

    def __post_init__(self) -> None:
        if not self.prefix.startswith(NAMESPACE_ROOT) or self.prefix == NAMESPACE_ROOT:
            raise NamespaceError(f"Namespace must be below {NAMESPACE_ROOT!r}")
        if validate_prefix(self.prefix) != self.prefix:
            raise NamespaceError(f"Namespace is not canonical: {self.prefix!r}")

    def child(self, name: str) -> Namespace:
        if not _CHILD_NAME.fullmatch(name):
            raise NamespaceError(f"Invalid child namespace name: {name!r}")
        return Namespace(f"{self.prefix}{name}/")

    def key(self, relative: str) -> str:
        key = validate_key(self.prefix + validate_key(relative))
        if not self.owns(key):
            raise NamespaceError(f"Key escapes the namespace: {relative!r}")
        return key

    def owns(self, key: str) -> bool:
        try:
            canonical = validate_key(key)
        except StorageError:
            return False
        return canonical == key and key.startswith(self.prefix)


@dataclass
class ObjectManifest:
    """In-memory list of objects the current test created, with last ETags."""

    namespace: Namespace
    _objects: dict[str, str | None] = field(default_factory=dict)

    def record(self, info: ObjectInfo) -> ObjectInfo:
        if not self.namespace.owns(info.key):
            raise NamespaceError(f"Refusing to track foreign key {info.key!r}")
        self._objects[info.key] = info.etag
        return info

    def forget(self, key: str) -> None:
        self._objects.pop(key, None)

    def items(self) -> list[tuple[str, str | None]]:
        return list(self._objects.items())

    def __len__(self) -> int:
        return len(self._objects)


@dataclass
class CleanupReport:
    deleted: list[str] = field(default_factory=list)
    already_missing: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failed


async def cleanup(storage: ObjectStorage, manifest: ObjectManifest) -> CleanupReport:
    """Delete exactly the objects in ``manifest``; never prefixes or folders.

    Each delete is conditional: with the last recorded ETag, or (if none was
    recorded) with the ETag the storage observes right before deleting. A
    key outside the namespace is refused without any request. Failures are
    collected, never raised, so one failure does not hide the others.
    """
    report = CleanupReport()
    for key, etag in manifest.items():
        if not manifest.namespace.owns(key):
            report.failed.append((key, "NamespaceError"))
            continue
        try:
            await storage.delete_object(key, if_match=etag)
        except NotFoundError:
            report.already_missing.append(key)
        except StorageError as exc:
            report.failed.append((key, type(exc).__name__))
        else:
            report.deleted.append(key)
        manifest.forget(key)
    return report
