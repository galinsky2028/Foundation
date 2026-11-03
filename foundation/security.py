"""Passwords, CSRF protection, login rate limiting, security headers and HTML sanitising."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

import nh3
from markupsafe import Markup
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .config import settings

CSRF_COOKIE = "app_csrf"
CSRF_FIELD = "csrf"

# --------------------------------------------------------------------------
# Passwords: scrypt from the Python standard library (no native dependency)
# --------------------------------------------------------------------------
_N, _R, _P = 2 ** 14, 8, 1
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
                            dklen=len(base64.b64decode(dk_b64)))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() in {"password1234", "1234567890", "qwertyuiop"}:
        return "That password is too common."
    return None


def token_hash(token: str) -> str:
    """Tokens (sessions, invitations, resets, API keys) are stored only as keyed hashes."""
    return hmac.new(settings.secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# Login rate limiting (per client address, in memory)
# --------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, max_events: int = 5, window_seconds: int = 900):
        self.max_events, self.window = max_events, window_seconds
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str) -> deque:
        q = self._events[key]
        while q and q[0] < time.time() - self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._trim(key)) >= self.max_events

    def hit(self, key: str) -> None:
        with self._lock:
            self._trim(key).append(time.time())

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


login_limiter = RateLimiter()
email_limiter = RateLimiter(max_events=5, window_seconds=3600)  # reset/invite emails per address


def client_ip(request: Request) -> str:
    # Behind Render's or Caddy's proxy the client address is the last X-Forwarded-For entry.
    # The header is trusted only with APP_TRUST_PROXY=true, because a direct client could forge it.
    fwd = request.headers.get("x-forwarded-for", "")
    if settings.trust_proxy and fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------
# CSRF: double-submit cookie. Every form carries the cookie's value; a page on
# another site can neither read the cookie nor guess the value.
# --------------------------------------------------------------------------
def csrf_field(request: Request) -> Markup:
    return Markup(f'<input type="hidden" name="{CSRF_FIELD}" value="{getattr(request.state, "csrf", "")}">')


async def csrf_protect(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    # JSON API calls authenticate with an API key header, not a cookie, so CSRF does not apply.
    if request.url.path.startswith("/api/") and request.headers.get("authorization", "").startswith("Bearer "):
        return
    from fastapi import HTTPException

    cookie = request.cookies.get(CSRF_COOKIE, "")
    sent = ""
    if "form" in request.headers.get("content-type", ""):
        sent = str((await request.form()).get(CSRF_FIELD, ""))
    sent = sent or request.headers.get("x-csrf-token", "")
    if not cookie or not sent or not secrets.compare_digest(cookie, sent):
        raise HTTPException(403, "This form expired or came from another site. Reload the page and try again.")


class SecurityMiddleware(BaseHTTPMiddleware):
    """CSRF cookie, request size cap and security headers on every response."""

    async def dispatch(self, request: Request, call_next):
        try:
            length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            length = 0
        if length > settings.max_upload_mb * 1024 * 1024 * 2:
            return PlainTextResponse(f"Request too large (limit {settings.max_upload_mb * 2} MB).", 413)
        token = request.cookies.get(CSRF_COOKIE)
        new = None
        if not token or len(token) < 20:
            token = new = secrets.token_urlsafe(24)
        request.state.csrf = token
        response = await call_next(request)
        if new:
            response.set_cookie(CSRF_COOKIE, new, httponly=True, samesite="strict", secure=settings.https,
                                max_age=60 * 60 * 24 * 30)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
                     "media-src 'self' https:; frame-src https://www.youtube-nocookie.com https://player.vimeo.com; "
                     "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
        if settings.https:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response


# --------------------------------------------------------------------------
# HTML sanitising for Markdown written by users, outside sources or a model
# --------------------------------------------------------------------------
ALLOWED_TAGS = {"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "u", "code", "pre",
                "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "a", "sup", "sub", "img"}


def sanitize_html(html: str) -> str:
    return nh3.clean(html, tags=ALLOWED_TAGS,
                     attributes={"a": {"href", "title"}, "img": {"src", "alt", "title"}, "td": {"align"}, "th": {"align"}},
                     url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer nofollow")


def render_markdown(text: str) -> str:
    import markdown as md

    return sanitize_html(md.markdown(text or "", extensions=["tables", "fenced_code"]))
