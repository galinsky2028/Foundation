"""The FastAPI application factory and the routes every project shares:
sign-in, registration and invitations, password reset, account settings, and the admin pages
for users, API keys, the audit log and background jobs.

Projects add their own routes in app/routes.py (a FastAPI APIRouter named `router`) and their own
templates in app/templates (which override Foundation's templates of the same name).
"""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import func, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import jobs
from .auth_core import (
    SESSION_COOKIE, LoginRequired, consume_token, create_api_key, create_user, current_user, end_all_sessions,
    end_session, get_user_by_email, issue_token, normalise_email, peek_token, require_role, require_user, start_session,
)
from .config import ROOT, settings
from .db import SessionLocal, audit, init_db, utcnow
from .mail import send_email
from .models import ROLES, ApiKey, AuditEvent, Job, User
from .security import (
    SecurityMiddleware, client_ip, csrf_field, csrf_protect, email_limiter, hash_password, login_limiter,
    password_problem, render_markdown, verify_password,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.loader = ChoiceLoader([FileSystemLoader(str(ROOT / "app" / "templates")),
                                     FileSystemLoader(str(HERE / "templates"))])
templates.env.globals.update(settings=settings, csrf_field=csrf_field, current_user=current_user)
templates.env.filters["markdown"] = render_markdown


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx or {}, status_code=status)


def safe_next(target: str | None) -> str:
    """Only same-site paths, so a crafted link cannot send someone to another site after login."""
    return target if target and target.startswith("/") and not target.startswith("//") else "/app"


# --------------------------------------------------------------------------
# Account routes
# --------------------------------------------------------------------------
accounts = APIRouter()


@accounts.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/app"):
    return render(request, "auth/login.html", {"next": safe_next(next), "error": ""})


@accounts.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/app")):
    ip = client_ip(request)
    if login_limiter.blocked(ip):
        return render(request, "auth/login.html", {"next": safe_next(next), "error": "Too many failed attempts. Wait 15 minutes."}, 429)
    with SessionLocal() as s:
        user = get_user_by_email(s, email)
        ok = user is not None and user.active and verify_password(password, user.password_hash)
        if not ok:
            login_limiter.hit(ip)
            audit(s, "auth.login_failed", None, target=normalise_email(email), ip=ip)
            s.commit()
            return render(request, "auth/login.html", {"next": safe_next(next), "error": "Wrong email or password."}, 401)
        audit(s, "auth.login", user.id, ip=ip)
        s.commit()
    login_limiter.reset(ip)
    resp = RedirectResponse(safe_next(next), status_code=303)
    resp.set_cookie(SESSION_COOKIE, start_session(user, ip), httponly=True, samesite="lax", secure=settings.https,
                    max_age=60 * 60 * 24 * settings.session_days)
    return resp


@accounts.post("/logout")
def logout(request: Request):
    end_session(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@accounts.get("/register", response_class=HTMLResponse)
def register_page(request: Request, token: str = ""):
    invite = peek_token(token, "invite") if token else None
    if settings.registration != "open" and invite is None:
        return render(request, "auth/message.html", {"title": "Registration is by invitation",
                      "message": "Ask an administrator for an invitation link."}, 403)
    return render(request, "auth/register.html", {"token": token, "email": invite.email if invite else "", "error": ""})


@accounts.post("/register")
def register(request: Request, email: str = Form(""), name: str = Form(""), password: str = Form(...),
             token: str = Form("")):
    ip = client_ip(request)
    if login_limiter.blocked(ip):
        raise HTTPException(429, "Too many attempts. Wait 15 minutes.")
    with SessionLocal() as s:
        role = "member"
        if token:
            invite = consume_token(s, token, "invite")
            if invite is None:
                return render(request, "auth/message.html", {"title": "Invitation expired",
                              "message": "This invitation link has expired or was already used."}, 400)
            email, role = invite.email, invite.role
        elif settings.registration != "open":
            raise HTTPException(403, "Registration is by invitation")
        problem = password_problem(password)
        if problem or "@" not in email:
            s.rollback()
            return render(request, "auth/register.html", {"token": token, "email": email,
                          "error": problem or "Enter a valid email address."}, 400)
        if get_user_by_email(s, email):
            login_limiter.hit(ip)
            s.rollback()
            return render(request, "auth/register.html", {"token": token, "email": email,
                          "error": "An account with that email already exists. Sign in or reset your password."}, 400)
        user = create_user(s, email, password, name=name, role=role)
        audit(s, "user.register", user.id, target=user.email, role=role, via="invite" if token else "open")
        s.commit()
    resp = RedirectResponse("/app", status_code=303)
    resp.set_cookie(SESSION_COOKIE, start_session(user, ip), httponly=True, samesite="lax", secure=settings.https,
                    max_age=60 * 60 * 24 * settings.session_days)
    return resp


@accounts.get("/reset", response_class=HTMLResponse)
def reset_request_page(request: Request):
    return render(request, "auth/reset_request.html", {"sent": False})


@accounts.post("/reset")
def reset_request(request: Request, email: str = Form(...)):
    email = normalise_email(email)
    # The same answer whether or not the account exists, so the form cannot be used to find accounts.
    if not email_limiter.blocked(email) and not email_limiter.blocked(client_ip(request)):
        email_limiter.hit(email)
        email_limiter.hit(client_ip(request))
        with SessionLocal() as s:
            user = get_user_by_email(s, email)
        if user and user.active:
            token = issue_token("reset", email, hours=1)
            send_email(email, f"Reset your {settings.app_name} password",
                       f"Someone asked to reset the password for {email} on {settings.app_name}.\n\n"
                       f"To choose a new password, open this link within one hour:\n{settings.base_url}/reset/{token}\n\n"
                       "If you did not ask for this, ignore this email; your password has not changed.")
    return render(request, "auth/reset_request.html", {"sent": True})


@accounts.get("/reset/{token}", response_class=HTMLResponse)
def reset_confirm_page(request: Request, token: str):
    if peek_token(token, "reset") is None:
        return render(request, "auth/message.html", {"title": "Link expired",
                      "message": "This reset link has expired or was already used. Request a new one."}, 400)
    return render(request, "auth/reset_confirm.html", {"token": token, "error": ""})


@accounts.post("/reset/{token}")
def reset_confirm(request: Request, token: str, password: str = Form(...)):
    problem = password_problem(password)
    if problem:
        return render(request, "auth/reset_confirm.html", {"token": token, "error": problem}, 400)
    with SessionLocal() as s:
        row = consume_token(s, token, "reset")
        user = get_user_by_email(s, row.email) if row else None
        if user is None:
            return render(request, "auth/message.html", {"title": "Link expired",
                          "message": "This reset link has expired or was already used."}, 400)
        user.password_hash = hash_password(password)
        audit(s, "user.password_reset", user.id)
        s.commit()
        uid = user.id
    end_all_sessions(uid)  # signs out every device that used the old password
    return render(request, "auth/message.html", {"title": "Password changed",
                  "message": "Your password was changed and all other sessions were signed out. You can sign in now."})


@accounts.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: User = Depends(require_user)):
    return render(request, "auth/account.html", {"user": user, "error": "", "saved": request.query_params.get("saved")})


@accounts.post("/account")
def account_save(request: Request, name: str = Form(""), current_password: str = Form(""), new_password: str = Form(""),
                 user: User = Depends(require_user)):
    with SessionLocal() as s:
        u = s.get(User, user.id)
        u.name = name.strip()[:200]
        if new_password:
            if not verify_password(current_password, u.password_hash):
                return render(request, "auth/account.html", {"user": u, "error": "Your current password is wrong."}, 400)
            problem = password_problem(new_password)
            if problem:
                return render(request, "auth/account.html", {"user": u, "error": problem}, 400)
            u.password_hash = hash_password(new_password)
            audit(s, "user.password_change", u.id)
        s.commit()
    return RedirectResponse("/account?saved=1", status_code=303)


# --------------------------------------------------------------------------
# Admin routes
# --------------------------------------------------------------------------
admin = APIRouter(prefix="/admin")


FLASH_COOKIE = "app_flash"


def set_flash(resp: Response, data: dict) -> Response:
    """One-time secrets (a new API key, an invitation link) are shown once through a short-lived,
    signed, HTTP-only cookie, never in the URL, where they would end up in server logs and history."""
    import json

    from itsdangerous import URLSafeTimedSerializer

    value = URLSafeTimedSerializer(settings.secret_key, salt="flash").dumps(json.dumps(data))
    resp.set_cookie(FLASH_COOKIE, value, max_age=120, httponly=True, samesite="strict", secure=settings.https, path="/admin")
    return resp


def take_flash(request: Request) -> dict:
    import json

    from itsdangerous import BadSignature, URLSafeTimedSerializer

    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return {}
    try:
        return json.loads(URLSafeTimedSerializer(settings.secret_key, salt="flash").loads(raw, max_age=120))
    except BadSignature:
        return {}


@admin.get("", response_class=HTMLResponse)
def admin_home(request: Request, user: User = Depends(require_role("admin"))):
    flash = take_flash(request)
    with SessionLocal() as s:
        users = s.scalars(select(User).order_by(User.created_at)).all()
        keys = s.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())).all()
        events = s.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(100)).all()
        job_rows = s.scalars(select(Job).order_by(Job.id.desc()).limit(30)).all()
        counts = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    from .llm import month_spend_usd

    resp = render(request, "admin/index.html", {
        "users": users, "keys": keys, "events": events, "jobs": job_rows, "job_counts": counts, "roles": ROLES,
        "new_key": flash.get("new_key", ""), "invite_link": flash.get("invite", ""),
        "spend": month_spend_usd()})
    if flash:
        resp.delete_cookie(FLASH_COOKIE, path="/admin")
    return resp


@admin.post("/invite")
def admin_invite(request: Request, email: str = Form(...), role: str = Form("member"),
                 user: User = Depends(require_role("admin"))):
    if role not in ROLES or "@" not in email:
        raise HTTPException(400, "Choose a role and a valid email address.")
    token = issue_token("invite", email, hours=24 * 7, role=role, created_by=user.id)
    link = f"{settings.base_url}/register?token={token}"
    send_email(normalise_email(email), f"You are invited to {settings.app_name}",
               f"{user.name or user.email} invited you to {settings.app_name} as {role}.\n\n"
               f"Create your account within seven days:\n{link}\n")
    with SessionLocal() as s:
        audit(s, "user.invite", user.id, target=normalise_email(email), role=role)
        s.commit()
    return set_flash(RedirectResponse("/admin#users", status_code=303), {"invite": link})


@admin.post("/users/{user_id}")
def admin_update_user(user_id: int, role: str = Form(...), active: str = Form(""),
                      user: User = Depends(require_role("admin"))):
    if role not in ROLES:
        raise HTTPException(400, "Unknown role")
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if u is None:
            raise HTTPException(404)
        if u.id == user.id and (role != "admin" or not active):
            raise HTTPException(400, "You cannot remove your own administrator access.")
        u.role, u.active = role, bool(active)
        audit(s, "user.update", user.id, target=u.email, role=role, active=u.active)
        s.commit()
    if not active:
        end_all_sessions(user_id)
    return RedirectResponse("/admin#users", status_code=303)


@admin.post("/keys")
def admin_create_key(name: str = Form(...), scopes: str = Form("*"), user: User = Depends(require_role("admin"))):
    key = create_api_key(name.strip() or "unnamed", [x.strip() for x in scopes.split(",") if x.strip()], user.id)
    return set_flash(RedirectResponse("/admin#keys", status_code=303), {"new_key": key})


@admin.post("/keys/{key_id}/revoke")
def admin_revoke_key(key_id: int, user: User = Depends(require_role("admin"))):
    with SessionLocal() as s:
        k = s.get(ApiKey, key_id)
        if k and k.revoked_at is None:
            k.revoked_at = utcnow()
            audit(s, "apikey.revoke", user.id, target=k.name)
            s.commit()
    return RedirectResponse("/admin#keys", status_code=303)


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------
def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db()
        jobs.start()
        yield
        jobs.stop()

    app = FastAPI(title=settings.app_name, lifespan=lifespan, dependencies=[Depends(csrf_protect)],
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(SecurityMiddleware)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app_static = ROOT / "app" / "static"
    if app_static.is_dir():
        app.mount("/app-static", StaticFiles(directory=str(app_static)), name="app-static")

    @app.middleware("http")
    async def refuse_public_without_secret(request: Request, call_next):
        # A public server must have a fixed APP_SECRET_KEY; otherwise every restart signs everyone out.
        if not settings.is_local and not settings.secret_key_from_env and request.url.path != "/health":
            return Response("APP_SECRET_KEY must be set before this service runs at a public address.", 503)
        return await call_next(request)

    app.include_router(accounts)
    app.include_router(admin)

    @app.get("/health")
    def health():
        return {"ok": True, "app": settings.app_name}

    try:
        from app.routes import router as project_router  # the project's own pages and API
        app.include_router(project_router)
    except ImportError as exc:
        if "app.routes" not in str(exc) and "No module named 'app'" not in str(exc):
            raise

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Sign-in required"}, 401)
        from urllib.parse import quote

        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, exc.status_code, headers=getattr(exc, "headers", None))
        return render(request, "errors/error.html", {"status": exc.status_code, "detail": exc.detail}, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.errors()}, 422)
        return render(request, "errors/error.html", {"status": 400, "detail": "Some required information was missing or invalid."}, 400)

    return app
