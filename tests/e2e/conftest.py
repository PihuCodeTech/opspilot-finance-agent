"""A live server on a fresh database with the demo cases, for browser tests (Playwright).

Uses the Chrome installed on the machine (`channel="chrome"`); CI installs Playwright's
Chromium instead. Needs TEST_DATABASE_URL, like the integration tests.
"""

import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import create_engine

from opspilot.config import AppEnv, get_settings
from opspilot.web.app import create_app
from opspilot.web.db import engine_transactions
from tests.e2e.demo_cases import BANK_KEY, Demo, build
from tests.integration.conftest import create_database, drop_database, point_settings_at, upgrade

PASSWORD = "demo-password-for-browser-tests"


@dataclass
class Server:
    url: str
    demo: Demo


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("TEST_DATABASE_URL is not set")
    url = create_database(base)
    store = tmp_path_factory.mktemp("documents")
    try:
        upgrade(url)
        with pytest.MonkeyPatch.context() as mp:
            point_settings_at(mp, url)
            engine = create_engine(url)
            demo = build(engine, Path(store), PASSWORD)
            settings = get_settings().model_copy(
                update={
                    "app_env": AppEnv.LOCAL,  # plain http on 127.0.0.1: cookies can't be Secure
                    "document_store_path": str(store),
                    "bank_hmac_key": SecretStr(BANK_KEY),
                }
            )
            app = create_app(engine_transactions(engine), settings=settings)
            port = _free_port()
            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            runner = uvicorn.Server(config)
            thread = threading.Thread(target=runner.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while not runner.started and time.monotonic() < deadline:
                time.sleep(0.05)
            yield Server(f"http://127.0.0.1:{port}", demo)
            runner.should_exit = True
            thread.join(timeout=5)
            engine.dispose()
    finally:
        drop_database(base, url)
