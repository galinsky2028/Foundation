"""Settings, read from environment variables (a `.env` file in the project root is loaded too).

Every project built on Foundation uses the same variable names, prefixed APP_. `.env.example`
documents each one.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def sqlalchemy_url(url: str) -> str:
    """postgres:// and postgresql:// (as Neon and Render print them) -> the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass
class Settings:
    # --- identity and branding (each project sets these) ---------------------------------------
    app_name: str = field(default_factory=lambda: env("APP_NAME", "Foundation"))
    app_tagline: str = field(default_factory=lambda: env("APP_TAGLINE", ""))
    organization: str = field(default_factory=lambda: env("APP_ORGANIZATION", "Galinsky Labs"))
    brand_color: str = field(default_factory=lambda: env("APP_BRAND_COLOR", "#2f80c8"))
    contact_email: str = field(default_factory=lambda: env("APP_CONTACT_EMAIL", ""))

    # --- web -------------------------------------------------------------------------------------
    base_url: str = field(default_factory=lambda: env("APP_BASE_URL", "http://localhost:8000").rstrip("/"))
    port: int = field(default_factory=lambda: env_int("PORT", 8000))
    secret_key: str = field(default_factory=lambda: env("APP_SECRET_KEY"))
    trust_proxy: bool = field(default_factory=lambda: env_bool("APP_TRUST_PROXY", False))
    session_days: int = field(default_factory=lambda: env_int("APP_SESSION_DAYS", 30))
    max_upload_mb: int = field(default_factory=lambda: env_int("APP_MAX_UPLOAD_MB", 50))
    # closed = only admins add users; invite = admins send invitation links; open = anyone may register
    registration: str = field(default_factory=lambda: env("APP_REGISTRATION", "invite"))
    # true only for testing against local servers: lets foundation.net fetch private addresses
    allow_private_urls: bool = field(default_factory=lambda: env_bool("APP_ALLOW_PRIVATE_URLS", False))

    # --- first administrator (created once, on first start, when no users exist) --------------
    admin_email: str = field(default_factory=lambda: env("APP_ADMIN_EMAIL"))
    admin_password: str = field(default_factory=lambda: env("APP_ADMIN_PASSWORD"))

    # --- storage ---------------------------------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(env("APP_DATA_DIR", str(ROOT / "data"))))
    database_url: str = field(default_factory=lambda: env("APP_DATABASE_URL") or env("DATABASE_URL"))
    database_url_direct: str = field(default_factory=lambda: env("APP_DATABASE_URL_DIRECT"))

    # --- email -----------------------------------------------------------------------------------
    email_backend: str = field(default_factory=lambda: env("APP_EMAIL_BACKEND", "console"))  # console | smtp
    email_from: str = field(default_factory=lambda: env("APP_EMAIL_FROM"))
    smtp_host: str = field(default_factory=lambda: env("APP_SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: env_int("APP_SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: env("APP_SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: env("APP_SMTP_PASSWORD"))
    smtp_starttls: bool = field(default_factory=lambda: env_bool("APP_SMTP_STARTTLS", True))

    # --- AI (optional) ---------------------------------------------------------------------------
    llm_provider: str = field(default_factory=lambda: env("APP_LLM_PROVIDER", "anthropic"))  # anthropic | mock
    anthropic_api_key: str = field(default_factory=lambda: env("ANTHROPIC_API_KEY"))
    llm_model: str = field(default_factory=lambda: env("APP_LLM_MODEL", "claude-sonnet-5-5"))
    llm_max_tokens: int = field(default_factory=lambda: env_int("APP_LLM_MAX_TOKENS", 4000))
    monthly_budget_usd: float = field(default_factory=lambda: env_float("APP_MONTHLY_BUDGET_USD", 20))

    # --- background worker -----------------------------------------------------------------------
    run_worker: bool = field(default_factory=lambda: env_bool("APP_RUN_WORKER", True))

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'app.db'}"
        self.database_url = sqlalchemy_url(self.database_url)
        self.database_url_direct = sqlalchemy_url(self.database_url_direct) if self.database_url_direct else ""
        if self.registration not in {"closed", "invite", "open"}:
            self.registration = "invite"
        self.secret_key_from_env = bool(self.secret_key)
        if not self.secret_key:
            # Development fallback only. On a server the key must be set, or every restart
            # (Render erases the disk) would sign everyone out and break emailed links.
            f = self.data_dir / ".secret_key"
            if not f.exists():
                f.write_text(secrets.token_hex(32))
            self.secret_key = f.read_text().strip()

    @property
    def https(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_local(self) -> bool:
        from urllib.parse import urlparse

        return (urlparse(self.base_url).hostname or "") in {"localhost", "127.0.0.1", "::1"}


settings = Settings()
