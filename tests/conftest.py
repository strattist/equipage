import os

import pytest
from aiohttp.test_utils import TestServer

os.environ.setdefault("ANTHROPIC_API_KEY", "test")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "agents.sqlite3")
    monkeypatch.setattr(config, "PROFILE_PATH", tmp_path / "profile.json")

    from app import db

    db.init()
    return tmp_path


@pytest.fixture
async def mealie():
    """A running fake Mealie and a client for it: yields (fake, client)."""
    from app.mealie import MealieClient
    from tests.fake_mealie import FakeMealie

    fake = FakeMealie(token="secret-token")
    server = TestServer(fake.app())
    await server.start_server()
    fake.base_url = str(server.make_url("/")).rstrip("/")
    async with MealieClient(fake.base_url, fake.token) as client:
        yield fake, client
    await server.close()
