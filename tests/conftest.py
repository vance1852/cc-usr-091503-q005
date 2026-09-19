import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.repository import Repo  # noqa: E402
from app.scheduler import Scheduler  # noqa: E402
from app.seed import seed  # noqa: E402
from app.timeutil import now_utc  # noqa: E402


@pytest.fixture
def now():
    return now_utc().replace(microsecond=0)


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def ctx(conn, now):
    data = seed(conn, now=now)
    return {
        "conn": conn,
        "repo": Repo(conn),
        "scheduler": Scheduler(Repo(conn)),
        "data": data,
        "now": now,
    }


@pytest.fixture
def client(conn, monkeypatch):
    """让 FastAPI 使用测试内存库。"""
    monkeypatch.setattr(api, "DB_PATH", ":memory:")
    app = api.app
    app.state.conn = conn
    init_db(conn)
    return TestClient(app)


@pytest.fixture
def seeded_client(client, now):
    data = seed(client.app.state.conn, now=now)
    return client, data
