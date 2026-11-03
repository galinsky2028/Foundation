import re

from conftest import csrf, last_email, login

from foundation.db import SessionLocal
from foundation.models import User, WebSession


def test_first_admin_is_created_and_can_sign_in(client):
    with SessionLocal() as s:
        admin = s.query(User).one()
        assert admin.email == "admin@example.com" and admin.role == "admin"
        assert admin.password_hash.startswith("scrypt$") and "correct horse" not in admin.password_hash
    login(client)
    assert client.get("/app").status_code == 200
    assert client.get("/admin").status_code == 200


def test_wrong_password_rate_limit_and_open_redirect(client):
    token = csrf(client)
    for _ in range(5):
        r = client.post("/login", data={"email": "admin@example.com", "password": "nope", "csrf": token})
        assert r.status_code == 401
    r = client.post("/login", data={"email": "admin@example.com", "password": "correct horse battery", "csrf": token})
    assert r.status_code == 429
    from foundation.security import login_limiter

    login_limiter._events.clear()
    r = client.post("/login", data={"email": "admin@example.com", "password": "correct horse battery",
                                    "next": "//evil.example", "csrf": token}, follow_redirects=False)
    assert r.headers["location"] == "/app"


def test_pages_need_login_and_csrf(client):
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    client.get("/login")
    assert client.post("/login", data={"email": "a", "password": "b"}).status_code == 403  # no csrf field


def test_invitation_flow(client):
    token = login(client)
    r = client.post("/admin/invite", data={"email": "Staff@Example.com", "role": "staff", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    mail = last_email()
    assert mail.startswith("staff@example.com")
    link = re.search(r"http://localhost:8000(/register\?token=\S+)", mail).group(1)
    client.post("/logout", data={"csrf": token})
    page = client.get(link)
    assert 'value="staff@example.com"' in page.text
    inv_token = link.split("token=")[1]
    r = client.post("/register", data={"token": inv_token, "name": "Sam", "password": "a long password!",
                                       "csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        u = s.query(User).filter_by(email="staff@example.com").one()
        assert u.role == "staff" and u.name == "Sam"
    # The invitation works once.
    r = client.post("/register", data={"token": inv_token, "password": "another long one", "csrf": token})
    assert r.status_code == 400


def test_registration_closed_without_invitation(client):
    assert client.get("/register").status_code == 403
    token = csrf(client)
    r = client.post("/register", data={"email": "x@example.com", "password": "a long password!", "csrf": token})
    assert r.status_code == 403


def test_password_reset_flow_signs_out_other_sessions(client):
    login(client)
    token = csrf(client, "/reset")
    r = client.post("/reset", data={"email": "nobody@example.com", "csrf": token})
    assert "If an account exists" in r.text  # same answer for unknown addresses
    client.post("/reset", data={"email": "admin@example.com", "csrf": token})
    link = re.search(r"http://localhost:8000(/reset/\S+)", last_email()).group(1)
    assert client.post(link, data={"password": "short", "csrf": token}).status_code == 400
    r = client.post(link, data={"password": "a brand new password", "csrf": token})
    assert "Password changed" in r.text
    with SessionLocal() as s:
        assert s.query(WebSession).count() == 0
    assert client.post(link, data={"password": "another new password", "csrf": token}).status_code == 400
    login(client, password="a brand new password")


def test_account_password_change_needs_current_password(client):
    token = login(client)
    r = client.post("/account", data={"name": "Phil", "current_password": "wrong", "new_password": "a new long password", "csrf": token})
    assert r.status_code == 400
    r = client.post("/account", data={"name": "Phil", "current_password": "correct horse battery",
                                      "new_password": "a new long password", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        assert s.query(User).one().name == "Phil"


def test_roles_deactivation_and_self_protection(client):
    from foundation.auth_core import create_user

    with SessionLocal() as s:
        m = create_user(s, "member@example.com", "member password 1")
        s.commit()
        mid = m.id
    token = login(client, "member@example.com", "member password 1")
    assert client.get("/admin").status_code == 403
    client.post("/logout", data={"csrf": token})
    token = login(client)
    with SessionLocal() as s:
        admin_id = s.query(User).filter_by(role="admin").one().id
    assert client.post(f"/admin/users/{admin_id}", data={"role": "member", "active": "1", "csrf": token}).status_code == 400
    client.post(f"/admin/users/{mid}", data={"role": "member", "csrf": token})  # unchecked = deactivate
    client.post("/logout", data={"csrf": token})
    token = csrf(client)
    r = client.post("/login", data={"email": "member@example.com", "password": "member password 1", "csrf": token})
    assert r.status_code == 401
