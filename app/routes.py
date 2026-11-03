"""This project's pages and API. Replace the examples below."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from foundation.auth_core import require_api_key, require_user
from foundation.models import ApiKey, User
from foundation.web import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    """The public front page."""
    return render(request, "home.html")


@router.get("/app", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_user)):
    """The signed-in starting page."""
    return render(request, "dashboard.html", {"user": user})


@router.get("/api/v1/ping")
def ping(key: ApiKey = Depends(require_api_key())):
    """Example API route: any valid API key may call it."""
    return {"ok": True, "key": key.name}
