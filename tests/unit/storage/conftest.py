from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from dav_helpers import PASSWORD, ROOT_URL, USERNAME, FakeNextcloud, Recorder
from polari_app.storage import NextcloudStorage, nextcloud


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Route every ``httpx.AsyncClient`` created during a test to a mock.

    ``NextcloudStorage`` creates its own client, so the mock transport is
    injected here. Because this fixture is autouse, no storage test can ever
    open a real network connection.
    """
    recorder = Recorder()
    real_client = httpx.AsyncClient

    def client_with_mock_transport(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recorder)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_mock_transport)
    return recorder


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nextcloud, "_PARENT_RETRY_DELAY", 0.0)


@pytest.fixture
def fake(recorder: Recorder) -> FakeNextcloud:
    fake = FakeNextcloud()
    recorder.handler = fake
    return fake


@pytest.fixture
async def storage() -> AsyncIterator[NextcloudStorage]:
    async with NextcloudStorage(ROOT_URL, USERNAME, PASSWORD) as storage:
        yield storage
