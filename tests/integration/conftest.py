import asyncio
import itertools
import os
from collections.abc import AsyncIterator

import pytest
from nextcloud_test_support import (
    CleanupReport,
    IntegrationConfig,
    IntegrationConfigError,
    Namespace,
    ObjectManifest,
    cleanup,
    new_run_id,
)
from polari_app.storage import NextcloudStorage, StorageError

_RUN_NAMESPACE = Namespace.for_run(new_run_id())
_TEST_COUNTER = itertools.count(1)
_CLEANUP_REPORTS: list[tuple[str, CleanupReport]] = []


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def integration_config() -> IntegrationConfig:
    try:
        return IntegrationConfig.from_env(os.environ)
    except IntegrationConfigError as exc:
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture
def namespace() -> Namespace:
    """A fresh namespace per test: ``integration-tests/<run-id>/t-0001/``."""
    return _RUN_NAMESPACE.child(f"t-{next(_TEST_COUNTER):04d}")


@pytest.fixture
def manifest(namespace: Namespace) -> ObjectManifest:
    return ObjectManifest(namespace)


@pytest.fixture
async def storage(
    integration_config: IntegrationConfig,
    manifest: ObjectManifest,
    request: pytest.FixtureRequest,
) -> AsyncIterator[NextcloudStorage]:
    config = integration_config
    async with NextcloudStorage(config.root_url, config.username, config.password) as s:
        try:
            yield s
        finally:
            _CLEANUP_REPORTS.append((request.node.nodeid, await cleanup(s, manifest)))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _CLEANUP_REPORTS:
        return
    write = terminalreporter.write_line
    terminalreporter.section("Nextcloud integration cleanup")
    deleted = sum(len(r.deleted) for _, r in _CLEANUP_REPORTS)
    missing = sum(len(r.already_missing) for _, r in _CLEANUP_REPORTS)
    failed = [(test, f) for test, r in _CLEANUP_REPORTS for f in r.failed]
    write(f"run namespace: {_RUN_NAMESPACE.prefix}")
    write(f"deleted: {deleted}, already missing: {missing}, failed: {len(failed)}")
    for test, (key, error) in failed:
        write(f"  FAILED {error}: {key} ({test})")
    write(f"objects left below the run namespace: {_count_leftovers()}")
    write("empty collections below the run namespace are left in place by design")


def _count_leftovers() -> str:
    try:
        config = IntegrationConfig.from_env(os.environ)
    except IntegrationConfigError:
        return "unknown (no valid config)"

    async def count() -> int:
        async with NextcloudStorage(
            config.root_url, config.username, config.password
        ) as storage:
            listing = await storage.list_objects(_RUN_NAMESPACE.prefix, delimiter=None)
            return len(listing.objects)

    try:
        return str(asyncio.run(count()))
    except StorageError as exc:
        return f"unknown ({type(exc).__name__})"
