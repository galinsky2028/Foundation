import httpx
import pytest
from conftest import csrf, login

from foundation import blobstore, jobs, llm
from foundation.auth_core import create_api_key
from foundation.client import ServiceClient, ServiceError
from foundation.db import SessionLocal
from foundation.models import Job
from foundation.security import hash_password, render_markdown, verify_password


def test_api_keys_scopes_and_revocation(client):
    assert client.get("/api/v1/ping").status_code == 401
    key = create_api_key("tester", ["*"])
    r = client.get("/api/v1/ping", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.json() == {"ok": True, "key": "tester"}
    assert client.get("/api/v1/ping", headers={"Authorization": f"Bearer {key}x"}).status_code == 401
    token = login(client)
    from foundation.models import ApiKey

    with SessionLocal() as s:
        kid = s.query(ApiKey).one().id
    client.post(f"/admin/keys/{kid}/revoke", data={"csrf": token})
    assert client.get("/api/v1/ping", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_scope_enforcement(db):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from foundation.auth_core import require_api_key

    app = FastAPI()

    @app.get("/api/x")
    def x(_k=Depends(require_api_key("records:write"))):
        return {"ok": True}

    c = TestClient(app)
    read_only = create_api_key("r", ["records:read"])
    writer = create_api_key("w", ["records:write"])
    assert c.get("/api/x", headers={"Authorization": f"Bearer {read_only}"}).status_code == 403
    assert c.get("/api/x", headers={"Authorization": f"Bearer {writer}"}).status_code == 200


def test_service_client_against_a_live_app(client):
    def via_testclient(request: httpx.Request) -> httpx.Response:
        r = client.request(request.method, str(request.url), headers=dict(request.headers), content=request.content)
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)

    transport = httpx.MockTransport(via_testclient)
    key = create_api_key("svc", ["*"])
    svc = ServiceClient("http://localhost", key, transport=transport, retries=0)
    assert svc.get("/api/v1/ping")["ok"] is True
    bad = ServiceClient("http://localhost", "fk_bad_bad", transport=transport, retries=0)
    with pytest.raises(ServiceError) as e:
        bad.get("/api/v1/ping")
    assert e.value.status == 401


def test_service_client_retries_transient_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200, json={"ok": len(calls)})

    svc = ServiceClient("http://svc", "k", transport=httpx.MockTransport(handler), retries=3)
    import foundation.client as fc

    fc.time.sleep = lambda s: None
    assert svc.get("/x") == {"ok": 3}


def test_jobs_run_once_retry_and_fail(db):
    seen = []

    @jobs.job("t_ok")
    def ok(payload, log):
        seen.append(payload["n"])
        log("ran")

    @jobs.job("t_bad")
    def bad(payload, log):
        raise ValueError("boom")

    a = jobs.enqueue("t_ok", {"n": 1})
    assert jobs.claim_next() == a
    assert jobs.claim_next() is None  # claimed exactly once
    jobs.run(a)
    b = jobs.enqueue("t_bad")
    for _ in range(jobs.MAX_ATTEMPTS):
        with SessionLocal() as s:
            s.get(Job, b).run_after = s.get(Job, b).created_at  # make the retry due now
            s.commit()
        jid = jobs.claim_next()
        jobs.run(jid)
    with SessionLocal() as s:
        assert s.get(Job, a).status == "done" and "ran" in s.get(Job, a).log
        bj = s.get(Job, b)
        assert bj.status == "failed" and bj.attempts == jobs.MAX_ATTEMPTS and "boom" in bj.error
    assert seen == [1]


def test_blobstore_and_safe_keys(db):
    key = blobstore.put(b"hello", "uploads/../../etc/passwd")
    assert key == "uploads/etc/passwd"
    assert blobstore.get(key) == (b"hello", "application/octet-stream")
    blobstore.put(b"a", "docs/1/a.pdf")
    assert blobstore.list_prefix("docs/1") == [("docs/1/a.pdf", 1)]
    blobstore.remove("docs/1/a.pdf")
    assert blobstore.get("docs/1/a.pdf") is None


def test_passwords_markdown_and_headers(client):
    h = hash_password("secret value 1")
    assert verify_password("secret value 1", h) and not verify_password("secret value 2", h)
    assert verify_password("x", "garbage") is False
    html = render_markdown("**hi** <script>alert(1)</script> [x](javascript:alert(1))")
    assert "<strong>hi</strong>" in html and "<script>" not in html and "javascript:" not in html
    r = client.get("/")
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "no-referrer"


def test_public_address_requires_secret_key(client, monkeypatch):
    from foundation.config import settings

    monkeypatch.setattr(settings, "base_url", "https://example.onrender.com")
    monkeypatch.setattr(settings, "secret_key_from_env", False)
    assert client.get("/").status_code == 503
    assert client.get("/health").status_code == 200


def test_llm_mock_and_budget(db, monkeypatch):
    from foundation.config import settings
    from foundation.models import LLMUsage

    llm.MockProvider.overrides["greet"] = lambda u: '{"hello": "world"}'
    assert llm.complete_json("greet", "s", "u") == {"hello": "world"}
    monkeypatch.setattr(settings, "monthly_budget_usd", 1.0)
    with SessionLocal() as s:
        s.add(LLMUsage(task="x", model="m", cost_usd=2.0))
        s.commit()
    with pytest.raises(llm.BudgetExceeded):
        llm.complete_text("greet", "s", "u")
    assert llm.cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == 12.0


def test_models_match_migrations(db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from foundation.db import Base, engine

    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata) == []


def test_error_pages_and_json_errors(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404 and "Page not found" in r.text
    r = client.get("/api/v1/nothing")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_new_api_key_is_shown_once_and_never_in_the_url(client):
    token = login(client)
    r = client.post("/admin/keys", data={"name": "engine caller", "scopes": "records:read", "csrf": token},
                    follow_redirects=False)
    assert r.status_code == 303 and "fk_" not in r.headers["location"]
    page = client.get("/admin")
    assert "Copy this key now" in page.text
    assert "Copy this key now" not in client.get("/admin").text  # shown only once
