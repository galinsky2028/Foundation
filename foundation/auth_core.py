"""Users, sessions, one-time tokens, API keys, and the FastAPI dependencies that use them.

Route dependencies:
    current_user(request)            -> User | None   (never raises)
    require_user                     -> User           (redirects to /login)
    require_role("staff")            -> User           (403 if the role is too low)
    require_api_key("scope")         -> ApiKey         (401/403 for JSON API routes)
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from .config import settings
from .db import SessionLocal, aware, audit, utcnow
from .models import ApiKey, OneTimeToken, User, WebSession
from .security import hash_password, token_hash

log = logging.getLogger(__name__)
SESSION_COOKIE = "app_session"


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def get_user_by_email(s, email: str) -> User | None:
    return s.scalar(select(User).where(User.email == normalise_email(email)))


def create_user(s, email: str, password: str, name: str = "", role: str = "member") -> User:
    u = User(email=normalise_email(email), name=name.strip()[:200], password_hash=hash_password(password), role=role)
    s.add(u)
    s.flush()
    return u


def ensure_first_admin() -> None:
    """On first start, create the administrator named by APP_ADMIN_EMAIL / APP_ADMIN_PASSWORD."""
    if not settings.admin_email or not settings.admin_password:
        return
    with SessionLocal() as s:
        if s.scalar(select(User.id).limit(1)) is not None:
            return
        u = create_user(s, settings.admin_email, settings.admin_password, name="Administrator", role="admin")
        audit(s, "user.bootstrap_admin", u.id, target=u.email)
        s.commit()
        log.info("created first administrator %s", u.email)


# --------------------------------------------------------------------------
# Sessions (random token in the cookie; only its hash in the database)
# --------------------------------------------------------------------------
def start_session(user: User, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    with SessionLocal() as s:
        s.add(WebSession(token_hash=token_hash(token), user_id=user.id, ip=ip[:64],
                         expires_at=utcnow() + timedelta(days=settings.session_days)))
        u = s.get(User, user.id)
        u.last_login_at = utcnow()
        s.commit()
    return token


def end_session(token: str | None) -> None:
    if not token:
        return
    with SessionLocal() as s:
        row = s.get(WebSession, token_hash(token))
        if row:
            s.delete(row)
            s.commit()


def end_all_sessions(user_id: int) -> None:
    with SessionLocal() as s:
        for row in s.scalars(select(WebSession).where(WebSession.user_id == user_id)).all():
            s.delete(row)
        s.commit()


def current_user(request: Request) -> User | None:
    if hasattr(request.state, "user"):
        return request.state.user
    user = None
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with SessionLocal() as s:
            row = s.get(WebSession, token_hash(token))
            if row is not None:
                if aware(row.expires_at) < utcnow():
                    s.delete(row)
                    s.commit()
                elif row.user.active:
                    user = row.user
    request.state.user = user
    return user


class LoginRequired(Exception):
    pass


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def require_role(*roles: str):
    def dep(user: User = Depends(require_user)) -> User:
        if not user.has_role(*roles):
            raise HTTPException(403, "You do not have permission to do that.")
        return user

    return dep


# --------------------------------------------------------------------------
# One-time tokens: invitations and password resets
# --------------------------------------------------------------------------
def issue_token(purpose: str, email: str, hours: int, role: str = "member", created_by: int | None = None) -> str:
    token = secrets.token_urlsafe(32)
    with SessionLocal() as s:
        s.add(OneTimeToken(token_hash=token_hash(token), purpose=purpose, email=normalise_email(email), role=role,
                           created_by=created_by, expires_at=utcnow() + timedelta(hours=hours)))
        s.commit()
    return token


def peek_token(token: str, purpose: str) -> OneTimeToken | None:
    with SessionLocal() as s:
        row = s.get(OneTimeToken, token_hash(token or ""))
        if row is None or row.purpose != purpose or row.used_at is not None or aware(row.expires_at) < utcnow():
            return None
        return row


def consume_token(s, token: str, purpose: str) -> OneTimeToken | None:
    row = s.get(OneTimeToken, token_hash(token or ""))
    if row is None or row.purpose != purpose or row.used_at is not None or aware(row.expires_at) < utcnow():
        return None
    row.used_at = utcnow()
    return row


# --------------------------------------------------------------------------
# API keys for service-to-service calls
# --------------------------------------------------------------------------
def create_api_key(name: str, scopes: list[str], created_by: int | None = None) -> str:
    """Returns the full key once. Format: fk_<prefix>_<secret>."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    full = f"fk_{prefix}_{secret}"
    with SessionLocal() as s:
        s.add(ApiKey(name=name[:200], prefix=prefix, key_hash=token_hash(full), scopes=sorted(set(scopes)),
                     created_by=created_by))
        audit(s, "apikey.create", created_by, target=name, scopes=scopes)
        s.commit()
    return full


def require_api_key(*scopes: str):
    """Dependency for /api routes. The caller sends: Authorization: Bearer fk_xxxx_yyyy"""

    def dep(request: Request) -> ApiKey:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer fk_"):
            raise HTTPException(401, "Missing API key", headers={"WWW-Authenticate": "Bearer"})
        full = auth[len("Bearer "):].strip()
        parts = full.split("_", 2)
        if len(parts) != 3:
            raise HTTPException(401, "Malformed API key")
        with SessionLocal() as s:
            key = s.scalar(select(ApiKey).where(ApiKey.prefix == parts[1], ApiKey.revoked_at.is_(None)))
            if key is None or not secrets.compare_digest(key.key_hash, token_hash(full)):
                raise HTTPException(401, "Invalid API key")
            if scopes and not (set(scopes) <= set(key.scopes or []) or "*" in (key.scopes or [])):
                raise HTTPException(403, f"API key lacks scope: {', '.join(scopes)}")
            key.last_used_at = utcnow()
            s.commit()
            return key

    return dep
