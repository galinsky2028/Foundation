import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="foundation_test_")
os.environ.update({
    "APP_DATA_DIR": _tmp,
    "APP_LLM_PROVIDER": "mock",
    "APP_EMAIL_BACKEND": "console",
    "APP_BASE_URL": "http://localhost:8000",
    "APP_RUN_WORKER": "false",
    "APP_REGISTRATION": "invite",
    "APP_ADMIN_EMAIL": "admin@example.com",
    "APP_ADMIN_PASSWORD": "correct horse battery",
    "APP_MONTHLY_BUDGET_USD": "0",
})
# CI also runs everything against a real Postgres server (the same engine Neon runs).
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["APP_DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import MetaData  # noqa: E402

from foundation.db import engine, init_db  # noqa: E402
from foundation.security import email_limiter, login_limiter  # noqa: E402


def reset_database():
    meta = MetaData()
    meta.reflect(engine)
    meta.drop_all(engine)
    init_db()


@pytest.fixture()
def db():
    reset_database()
    login_limiter._events.clear()
    email_limiter._events.clear()
    yield


@pytest.fixture()
def client(db):
    from main import app

    with TestClient(app, base_url="http://localhost") as c:
        yield c


def csrf(client, path="/login") -> str:
    import re

    r = client.get(path)
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    return m.group(1)


def login(client, email="admin@example.com", password="correct horse battery"):
    token = csrf(client)
    r = client.post("/login", data={"email": email, "password": password, "csrf": token}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return token


def last_email() -> str:
    from foundation.config import settings

    files = sorted((settings.data_dir / "outbox").glob("*.eml"))
    import email

    msg = email.message_from_bytes(files[-1].read_bytes())
    body = msg.get_body(preferencelist=("plain",)) if hasattr(msg, "get_body") else msg
    from email import policy

    msg = email.message_from_bytes(files[-1].read_bytes(), policy=policy.default)
    return msg["To"] + "\n" + msg.get_body(preferencelist=("plain",)).get_content()
